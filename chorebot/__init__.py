"""Suite chore bot. See design.md."""

from pathlib import Path

from dotenv import load_dotenv

# Runs on package import because several modules read the environment at import time.
# The explicit path keeps this independent of the working directory and launch method.
# Real environment variables win over .env, so host settings are never overridden.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
