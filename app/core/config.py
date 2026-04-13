
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Paths
PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
CHROMA_DIR = DATA_DIR / "chroma_db"
SCREENSHOT_DIR = DATA_DIR / "screenshots"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
CHROMA_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# LLM
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_TEMPERATURE = 0.1  
LLM_MAX_RETRIES = 2

# Embeddings (local, free)
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# Extraction tuning
MAX_SLIDES_TO_PROCESS = 40
MAX_CODE_FILES = 30          
MAX_FILE_SIZE_KB = 80        
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs",
    ".java", ".rb", ".php", ".cs", ".cpp", ".c", ".h",
    ".html", ".css", ".vue", ".svelte",
}
IMPORTANT_FILES = {
    "README.md", "readme.md", "package.json", "requirements.txt",
    "pyproject.toml", "Cargo.toml", "go.mod", "Dockerfile",
}

# Scoring
RUBRIC_CRITERIA = [
    "Problem Understanding",
    "Technical Approach",
    "Implementation Quality",
    "Innovation / Originality",
    "Communication & Demo Clarity",
]

# Playwright
PROTOTYPE_TIMEOUT_MS = 15000
PROTOTYPE_MAX_CLICKS = 5     # safety cap

# Retrieval
EVIDENCE_TOP_K = 8
