# run_session_duration_test.py

# --- Start of Python Test Script Code ---
from tasks import check_schedulesource_session_task # Imports your Celery task
# from celery.result import AsyncResult # Uncomment if you want to check results synchronously later
import time
import datetime

USER_ID_TO_TEST = 1 # !!! IMPORTANT: Change this to your actual test user's ID !!!

# --- Choose your check interval ---
# CHECK_INTERVAL_SECONDS = 30  # Very frequent for initial testing (30 seconds)
CHECK_INTERVAL_SECONDS = 60 * 5 # Every 5 minutes
# CHECK_INTERVAL_SECONDS = 60 * 15 # Every 15 minutes
# CHECK_INTERVAL_SECONDS = 60 * 60 # Every 1 hour

MAX_CHECKS = 288 # e.g., for 5-min intervals over 24 hours (24*12=288), or 24 for 1-hour intervals

def run_test(): # Encapsulate in a function to keep imports clean at top level
    print(f"--- Session Duration Test Script ---")
    print(f"Starting test for User ID: {USER_ID_TO_TEST}")
    print(f"Check interval: {CHECK_INTERVAL_SECONDS / 60.0:.2f} minutes")
    print(f"Initial Duo setup for this session should have just been completed.")
    print(f"Current time: {datetime.datetime.now().isoformat()}")
    print("Remember to monitor your Celery worker logs for 'SESSION_CHECK_RESULT'.")
    print("-" * 30)

    # Optional: Initial check immediately
    print(f"[{datetime.datetime.now().isoformat()}] Queueing initial immediate session check for user {USER_ID_TO_TEST}...")
    try:
        task_result_initial = check_schedulesource_session_task.delay(USER_ID_TO_TEST)
        print(f"Initial check task ID: {task_result_initial.id}. Check Celery logs.")
    except Exception as e_initial_task:
        print(f"ERROR queueing initial task: {e_initial_task}")
        print("Please ensure 'check_schedulesource_session_task' is defined correctly in tasks.py and Celery worker is running.")
        return # Exit if initial task can't be queued

    try:
        for i in range(MAX_CHECKS):
            print(f"\n[{datetime.datetime.now().isoformat()}] Iteration {i+1}/{MAX_CHECKS}. Waiting {CHECK_INTERVAL_SECONDS / 60.0:.1f} minutes for next check...")
            time.sleep(CHECK_INTERVAL_SECONDS)

            print(f"[{datetime.datetime.now().isoformat()}] Queueing periodic session check for user {USER_ID_TO_TEST}...")
            task_result_periodic = check_schedulesource_session_task.delay(USER_ID_TO_TEST)
            print(f"Periodic check task ID: {task_result_periodic.id}. Check Celery logs.")

            # Optional synchronous result checking block (keep commented unless needed for specific debug)
            # ... (the commented out AsyncResult block from previous snippet) ...

    except KeyboardInterrupt:
        print("\n--- Test script interrupted by user. ---")
    except NameError as ne: # Catch if check_schedulesource_session_task is still not found during loop
        print(f"NameError during loop: {ne}. Is the task correctly imported?")
    finally:
        print("--- Session duration test script finished or interrupted. ---")

if __name__ == "__main__":
    # This allows celery_init.py to do its sys.path modification correctly if it's being
    # run because 'tasks' (which imports celery_init indirectly) is imported first.
    # We also need to make sure that if this script is run, the PROJECT_ROOT logic
    # from celery_init.py has a chance to run before 'tasks' is imported.
    # The current working directory *should* be the project root when you run this.
    run_test()
# --- End of Python Test Script Code ---