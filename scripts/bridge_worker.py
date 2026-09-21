"""Stdlib-only entry usable by Windows Python and WSL Python alike."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codex_model_manager.core.managed_bridge import worker_main
if __name__ == '__main__':
    raise SystemExit(worker_main())
