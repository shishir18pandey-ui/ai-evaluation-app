
import asyncio
import logging
import re
from typing import Optional

from app.core.models import Evidence, PrototypeValidation
from app.core import config
from app.llm import llm_json

logger = logging.getLogger(__name__)


KEYWORD_MAPPER_SYSTEM = """You map a claimed feature to DOM search keywords that would prove the feature exists.

Given a claimed feature, return 2-4 lowercase keywords that should appear in button text,
link text, input placeholder, or visible page text if the feature is real.

Example inputs and outputs:
- "user authentication" → {"keywords": ["login", "sign in", "log in", "signup"]}
- "AI summarization" → {"keywords": ["summarize", "summary", "generate summary", "ai"]}
- "export to CSV" → {"keywords": ["export", "download", "csv"]}

Return JSON: {"keywords": ["kw1", "kw2", ...]}
Do not suggest generic words like "button" or "click".
"""


async def _safe_goto(page, url: str, timeout: int) -> tuple[bool, Optional[str]]:
    try:
        await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        return True, None
    except Exception as e:
        return False, str(e)


async def _snapshot_page(page) -> dict:
    try:
        title = await page.title()
    except Exception:
        title = ""

    elements = await page.evaluate("""
        () => {
            const pick = (el) => ({
                tag: el.tagName.toLowerCase(),
                text: (el.innerText || el.value || el.placeholder || '').trim().slice(0, 100),
                type: el.type || '',
                id: el.id || '',
                className: (el.className || '').toString().slice(0, 80),
                placeholder: el.placeholder || '',
            });
            const buttons = Array.from(document.querySelectorAll('button, [role=button], input[type=submit], input[type=button]'))
                .filter(e => e.offsetParent !== null).slice(0, 30).map(pick);
            const links = Array.from(document.querySelectorAll('a[href]'))
                .filter(e => e.offsetParent !== null && e.innerText.trim()).slice(0, 30).map(pick);
            const inputs = Array.from(document.querySelectorAll('input, textarea, select'))
                .filter(e => e.offsetParent !== null).slice(0, 20).map(pick);
            const bodyText = document.body.innerText.slice(0, 2000).toLowerCase();
            return { buttons, links, inputs, bodyText };
        }
    """)
    return {"title": title, **elements}


def _find_matching_element(snapshot: dict, keywords: list[str]) -> dict | None:
    keywords_lower = [k.lower().strip() for k in keywords if k.strip()]
    if not keywords_lower:
        return None
    for source_name, items in [
        ("button", snapshot.get("buttons", [])),
        ("link", snapshot.get("links", [])),
        ("input", snapshot.get("inputs", [])),
    ]:
        for item in items:
            searchable = " ".join([
                item.get("text", ""), item.get("id", ""),
                item.get("className", ""), item.get("placeholder", ""),
            ]).lower()
            for kw in keywords_lower:
                if kw in searchable:
                    return {"element_type": source_name, "element": item, "matched_keyword": kw}
    return None


def _body_mentions(snapshot: dict, keywords: list[str]) -> list[str]:
    body = snapshot.get("bodyText", "").lower()
    return [kw for kw in keywords if kw.lower() in body]


async def _test_feature(page, url: str, feature: str, snapshot: dict) -> dict:
    try:
        kw_result = llm_json(KEYWORD_MAPPER_SYSTEM, f"FEATURE: {feature}\n\nSuggest keywords.")
        keywords = kw_result.get("keywords", []) if isinstance(kw_result, dict) else []
    except Exception as e:
        logger.warning("Keyword mapping failed for %s: %s", feature, e)
        keywords = [w for w in re.findall(r"\w+", feature.lower()) if len(w) > 3]

    if not keywords:
        return {"feature": feature, "status": "not_tested",
                "evidence": "Could not derive search keywords", "keywords_searched": []}

    match = _find_matching_element(snapshot, keywords)

    if match is None:
        body_mentions = _body_mentions(snapshot, keywords)
        if body_mentions:
            return {"feature": feature, "status": "not_found",
                    "evidence": f"Feature mentioned in page text (keywords: {body_mentions}) but no interactive element found.",
                    "keywords_searched": keywords}
        return {"feature": feature, "status": "not_found",
                "evidence": f"No DOM element matches keywords {keywords}. Inspected {len(snapshot.get('buttons', []))} buttons, {len(snapshot.get('links', []))} links, {len(snapshot.get('inputs', []))} inputs.",
                "keywords_searched": keywords}

    element_type = match["element_type"]
    element = match["element"]
    matched_kw = match["matched_keyword"]
    element_text = element.get("text", "") or element.get("placeholder", "")

    if element_type == "input":
        return {"feature": feature, "status": "working",
                "evidence": f"Interactive input found: <{element['tag']}> placeholder='{element.get('placeholder', '')}' matched keyword '{matched_kw}'.",
                "keywords_searched": keywords}

    try:
        # If there's a text input on the page, fill it first so the click has something to act on
        inputs = snapshot.get("inputs", [])
        filled_input = None
        if inputs:
            try:
                first_input = inputs[0]
                placeholder = first_input.get("placeholder", "")
                if placeholder:
                    await page.get_by_placeholder(placeholder, exact=False).first.fill(
                        "test note content", timeout=2000
                    )
                    filled_input = placeholder
            except Exception:
                pass  # input fill is best-effort

        # Capture rich pre-click state
        pre_state = await page.evaluate("""
            () => ({
                html_len: document.documentElement.outerHTML.length,
                body_text_len: document.body.innerText.length,
                li_count: document.querySelectorAll('li').length,
                div_count: document.querySelectorAll('div').length,
                list_items_text: Array.from(document.querySelectorAll('li, .note, .item'))
                    .map(e => e.innerText.trim()).join('|'),
            })
        """)

        await page.get_by_text(element_text, exact=False).first.click(timeout=3000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        await asyncio.sleep(0.5)

        post_state = await page.evaluate("""
            () => ({
                html_len: document.documentElement.outerHTML.length,
                body_text_len: document.body.innerText.length,
                li_count: document.querySelectorAll('li').length,
                div_count: document.querySelectorAll('div').length,
                list_items_text: Array.from(document.querySelectorAll('li, .note, .item'))
                    .map(e => e.innerText.trim()).join('|'),
            })
        """)
        new_title = await page.title()

        # Multi-signal change detection
        html_changed = abs(post_state["html_len"] - pre_state["html_len"]) > 10
        body_text_changed = abs(post_state["body_text_len"] - pre_state["body_text_len"]) > 5
        list_items_added = post_state["li_count"] > pre_state["li_count"]
        list_content_changed = post_state["list_items_text"] != pre_state["list_items_text"]
        title_changed = new_title != snapshot.get("title", "")

        any_change = (html_changed or body_text_changed or list_items_added
                      or list_content_changed or title_changed)

        if any_change:
            signals = []
            if list_items_added:
                signals.append(f"list items {pre_state['li_count']}→{post_state['li_count']}")
            if body_text_changed:
                signals.append(f"body text {pre_state['body_text_len']}→{post_state['body_text_len']} chars")
            if html_changed:
                signals.append(f"HTML {pre_state['html_len']}→{post_state['html_len']} chars")
            if title_changed:
                signals.append(f"title changed")
            fill_note = f" (filled input '{filled_input}' first)" if filled_input else ""
            return {
                "feature": feature, "status": "working",
                "evidence": (f"Clicked <{element['tag']}> '{element_text}' "
                             f"(matched '{matched_kw}'){fill_note}. "
                             f"DOM reacted: {', '.join(signals)}."),
                "keywords_searched": keywords,
            }
        else:
            return {
                "feature": feature, "status": "broken",
                "evidence": (f"Clicked <{element['tag']}> '{element_text}' but NO DOM change "
                             f"detected across html/body/list/title. Button exists but inert."),
                "keywords_searched": keywords,
            }
    except Exception as e:
        return {"feature": feature, "status": "broken",
                "evidence": f"Found element <{element['tag']}> '{element_text}' but interaction failed: {e}",
                "keywords_searched": keywords}  

async def validate_prototype_async(url: str, features_to_test: list[str]) -> PrototypeValidation:
    from playwright.async_api import async_playwright

    if not url or not url.startswith(("http://", "https://")):
        return PrototypeValidation(url=url, accessible=False, errors=["Invalid URL"])

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (compatible; EvidenceLayerBot/1.0)",
        )
        page = await context.new_page()

        ok, err = await _safe_goto(page, url, config.PROTOTYPE_TIMEOUT_MS)
        if not ok:
            await browser.close()
            return PrototypeValidation(url=url, accessible=False, errors=[err or "Load failed"])

        snapshot = await _snapshot_page(page)
        screenshot_path = config.SCREENSHOT_DIR / "homepage.png"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=False)
        except Exception:
            pass

        feature_results = []
        tested_features = features_to_test[: config.PROTOTYPE_MAX_CLICKS]
        for feature in tested_features:
            await _safe_goto(page, url, config.PROTOTYPE_TIMEOUT_MS)
            snap = await _snapshot_page(page)
            result = await _test_feature(page, url, feature, snap)
            feature_results.append(result)

        await browser.close()

        return PrototypeValidation(
            url=url, accessible=True, page_title=snapshot.get("title", ""),
            features_tested=feature_results,
            screenshots=[str(screenshot_path)] if screenshot_path.exists() else [],
        )


def validate_prototype(url: str, features_to_test: list[str]) -> PrototypeValidation:
    return asyncio.run(validate_prototype_async(url, features_to_test))


def prototype_to_evidence(validation: PrototypeValidation) -> list[Evidence]:
    if not validation.accessible:
        return [Evidence(
            source_type="url", source_id="url_load",
            claim_or_fact="Prototype URL inaccessible",
            evidence_text=f"Failed to load {validation.url}: {'; '.join(validation.errors)}",
            confidence=0.95, metadata={"accessible": False},
        )]

    evidences = [Evidence(
        source_type="url", source_id="url_home",
        claim_or_fact=f"Prototype loads: '{validation.page_title}'",
        evidence_text=f"Homepage loaded at {validation.url}. Title: {validation.page_title}",
        confidence=0.9, metadata={"accessible": True},
    )]

    for ft in validation.features_tested:
        status = ft["status"]
        feature = ft["feature"]

        if status == "working":
            claim_text = f"Feature '{feature}' works in the prototype"
            confidence = 0.85
        elif status == "not_found":
            claim_text = f"Feature '{feature}' is NOT PRESENT in the prototype"
            confidence = 0.9
        elif status == "broken":
            claim_text = f"Feature '{feature}' exists in prototype but does not function"
            confidence = 0.85
        else:
            claim_text = f"Feature '{feature}' could not be tested"
            confidence = 0.4

        evidences.append(Evidence(
            source_type="url",
            source_id=f"url_test_{feature[:40].replace(' ', '_')}",
            claim_or_fact=claim_text,
            evidence_text=f"{status.upper()}: {ft.get('evidence', '')}",
            confidence=confidence,
            metadata={
                "feature": feature, "status": status,
                "keywords_searched": ", ".join(ft.get("keywords_searched", []))[:200],
            },
        ))
    return evidences