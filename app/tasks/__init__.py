from pathlib import Path

from funboost import BoosterDiscovery

# BoosterDiscovery(project_root_path=Path(__file__).parent, booster_dirs=[Path(__file__).parent]).auto_discovery()
from app.tasks import task_syncer

task_syncer.syncer.consume()
task_syncer.reload.consume()
