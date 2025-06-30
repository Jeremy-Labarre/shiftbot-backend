# worker.py
from celery_init import celery_app

# NO OTHER CODE, especially NO eventlet.monkey_patch()

if __name__ == "__main__":
    # This is a fallback, direct celery command is better for specifying pools
    argv = ['worker', '--loglevel=INFO']
    celery_app.worker_main(argv)