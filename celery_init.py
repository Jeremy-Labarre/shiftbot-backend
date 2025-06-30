# celery_init.py
import os
import sys
import asyncio
if sys.platform == "win32" and sys.version_info >= (3, 8):
    print(">>> celery_init.py: Setting WindowsProactorEventLoopPolicy for asyncio.")
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


from celery import Celery
from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
    print(f">>> celery_init.py: Added project root '{PROJECT_ROOT}' to sys.path.")
else:
    print(f">>> celery_init.py: Project root '{PROJECT_ROOT}' already in sys.path.")

load_dotenv()



celery_app = Celery('tasks_module_name', # Can be anything, but often project name or 'tasks'
                    broker=os.getenv("REDIS_URL"),
                    backend=os.getenv("REDIS_URL"),
                    # Add include to explicitly tell Celery where to find tasks.
                    # This is often more robust than relying solely on 'imports' in conf.
                    include=['tasks']) # Ensure 'tasks' refers to tasks.py

celery_app.conf.update(
    # imports=('tasks',), # 'include' in Celery() constructor is preferred for task discovery
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    broker_connection_retry_on_startup=True,
)