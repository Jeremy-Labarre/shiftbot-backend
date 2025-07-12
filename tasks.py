import os
import sys
import asyncio
import re
import json
import traceback
# import time # Only explicitly used for time.strftime, consider dt.datetime.strftime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
from playwright.async_api import Page, Frame, async_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
from celery.utils.log import get_task_logger
from celery.schedules import crontab
import redis
import requests
import msal

from celery_init import celery_app
from models import User, ScrapedShift, ShiftConfig, datetime_utc # datetime_utc is a function now

load_dotenv()
logger = get_task_logger(__name__)

# --- Database Setup ---
SQLALCHEMY_DATABASE_URI_FROM_ENV = os.getenv("SQLALCHEMY_DATABASE_URI")
if not SQLALCHEMY_DATABASE_URI_FROM_ENV:
    logger.critical("CRITICAL: SQLALCHEMY_DATABASE_URI not found!")
    raise RuntimeError("DB URI not set for Celery worker")
logger.info(f"--- [Celery Worker tasks.py] Using SQLALCHEMY_DATABASE_URI: {SQLALCHEMY_DATABASE_URI_FROM_ENV}")
ENGINE = create_engine(SQLALCHEMY_DATABASE_URI_FROM_ENV)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=ENGINE)

# --- Redis for Cache ---
try:
    redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6380/0"), decode_responses=True)
    redis_client.ping(); logger.info("Successfully connected to Redis for caching.")
except Exception as e_redis: logger.error(f"Could not connect to Redis: {e_redis}."); redis_client = None

# --- Playwright & MSAL Constants (same as your latest provided version) ---
SWAP_URL = "https://uge.schedulesource.net/TeamWork5/Emp/Sch/#SwapBoard"
USER_DATA_ROOT_DIR = "userdata_celery"; os.makedirs(USER_DATA_ROOT_DIR, exist_ok=True)
STORAGE_STATE_FILENAME = "storage_state.json"
ROW_SEL = "tr[data-uid]"; MASK_SEL = ".k-loading-mask:visible"
PAGESIZE_WRAP_SEL = "span[aria-label='Page sizes drop down']"
KENDO_ANIMATION_CONTAINER_VISIBLE = "div.k-animation-container[style*='display: block']"
LI_ALL_SEL = f"{KENDO_ANIMATION_CONTAINER_VISIBLE} li:has-text('All')"
PERIOD_WRAP_SEL = "span.k-dropdownlist:not([aria-label])"
LI_MON_SEL = f"{KENDO_ANIMATION_CONTAINER_VISIBLE} li:has-text('Month')"
NEXT_PERIOD_BUTTON_SEL = "button[data-bind*='NextDate']"
RECENTLY_SEEN_CACHE_PREFIX = "seen_shifts_user_"
RECENTLY_SEEN_CACHE_EXPIRY_SECONDS = 60 * 60 * 4
QUICK_CHECK_INTERVAL_SECONDS = 10
QUICK_CHECK_MAX_ROWS = None
MSAL_CLIENT_ID = os.getenv("MSAL_CLIENT_ID", "0b2e1a3a-6cdc-44f6-85a6-b19a5b772ed3")
MSAL_AUTHORITY = os.getenv("MSAL_AUTHORITY", "https://login.microsoftonline.com/consumers")
MSAL_SCOPES = ["Mail.Send"]
CELERY_TOKEN_CACHE_DIR = os.path.join(USER_DATA_ROOT_DIR, ".mscache_celery")
os.makedirs(CELERY_TOKEN_CACHE_DIR, exist_ok=True)
CELERY_MSAL_TOKEN_CACHE_FILE = os.path.join(CELERY_TOKEN_CACHE_DIR, "celery_worker_token_cache.bin")
BOT_SENDER_EMAIL = os.getenv("BOT_SENDER_EMAIL")

_msal_app_instance = None
_msal_token_cache = None

def _get_msal_app_and_cache(): # ... (no changes from your version) ...
    global _msal_app_instance, _msal_token_cache
    if _msal_token_cache is None:
        _msal_token_cache = msal.SerializableTokenCache()
        if os.path.exists(CELERY_MSAL_TOKEN_CACHE_FILE):
            try: _msal_token_cache.deserialize(open(CELERY_MSAL_TOKEN_CACHE_FILE, "r").read()); logger.info(f"MSAL token cache loaded from {CELERY_MSAL_TOKEN_CACHE_FILE}")
            except Exception as e: logger.error(f"Failed to load MSAL token cache: {e}.")
    if _msal_app_instance is None: _msal_app_instance = msal.PublicClientApplication(MSAL_CLIENT_ID, authority=MSAL_AUTHORITY, token_cache=_msal_token_cache)
    return _msal_app_instance, _msal_token_cache

def acquire_celery_worker_token(): # ... (no changes from your version with removed has_changed) ...
    if not BOT_SENDER_EMAIL: logger.error("BOT_SENDER_EMAIL not configured."); return None
    app, cache = _get_msal_app_and_cache(); accounts = app.get_accounts()
    if accounts:
        target_account = next((acc for acc in accounts if acc.get("username","").lower() == BOT_SENDER_EMAIL.lower()), accounts[0] if accounts else None)
        if target_account:
            logger.info(f"MSAL: Attempting silent token for {target_account.get('username')}.")
            result = app.acquire_token_silent(MSAL_SCOPES, account=target_account)
            if result and "access_token" in result:
                logger.info(f"MSAL: Silently acquired token for {target_account.get('username')}.")
                try:
                    with open(CELERY_MSAL_TOKEN_CACHE_FILE,"w") as f:f.write(cache.serialize())
                    logger.info(f"MSAL cache updated (silent) at {CELERY_MSAL_TOKEN_CACHE_FILE}")
                except Exception as e:logger.error(f"Failed to save MSAL cache (silent): {e}")
                return result["access_token"]
            else: failure_reason=result.get("error_description","Unknown (no desc).") if result else "Unknown (result None)."
            logger.warning(f"MSAL: Silent token failed for {target_account.get('username')}. Reason: {failure_reason}")
        else: logger.warning(f"MSAL: No account in cache matched BOT_SENDER_EMAIL ('{BOT_SENDER_EMAIL}').")
    logger.warning("MSAL: Initiating device flow for Celery worker..."); flow=app.initiate_device_flow(scopes=MSAL_SCOPES)
    if "message" not in flow: logger.error(f"MSAL: Device flow init failed: {flow}"); return None
    logger.critical(f"MSAL DEVICE FLOW: {flow['message']} --- COMPLETE THIS IN BROWSER USING '{BOT_SENDER_EMAIL}' ACCOUNT. ---")
    result_df=None
    try: result_df=app.acquire_token_by_device_flow(flow,timeout=180)
    except Exception as e_df: logger.error(f"MSAL: Error/timeout during device_flow: {e_df}")
    try: # Always try to save cache after device flow attempt
        with open(CELERY_MSAL_TOKEN_CACHE_FILE,"w") as f:f.write(cache.serialize())
        logger.info(f"MSAL cache saved to {CELERY_MSAL_TOKEN_CACHE_FILE} after device flow attempt.")
    except Exception as e_cs: logger.error(f"Failed to save MSAL cache post-device_flow: {e_cs}")
    if result_df and "access_token" in result_df: logger.info(f"MSAL: Token via device flow for {BOT_SENDER_EMAIL}.");return result_df["access_token"]
    else: logger.error(f"MSAL: Device flow no access token. Response: {result_df}");return None

# --- Playwright Async Helpers (using your latest refined versions) ---
def norm(t: str) -> str: return " ".join(t.split()).strip()
async def _find_target_frame_for_grid(page: Page, task_name: str = "Operations") -> Page | Frame: # ... (No changes from your version, assumed correct with PlaywrightTimeoutError alias) ...
    logger.info(f"[{task_name}] Finding target frame/page. Initial page URL: {page.url}")
    await page.wait_for_timeout(500)
    for _try_count_iframe in range(2):
        for frame in page.frames:
            try:
                if frame.url and frame.url.endswith("/SwapBoard/Index"):
                    await frame.wait_for_selector(ROW_SEL, timeout=3000, state="visible")
                    logger.info(f"✔ [{task_name}] Found target iframe by URL suffix '/SwapBoard/Index': {frame.name or frame.url}")
                    return frame
            except PlaywrightTimeoutError: logger.debug(f"[{task_name}] Frame {frame.url if frame and hasattr(frame, 'url') else 'N/A'} with /SwapBoard/Index found, but no rows quickly.")
            except Exception as e: logger.debug(f"[{task_name}] Error checking frame by URL: {e}")
        if _try_count_iframe == 0: await page.wait_for_timeout(500)
    base_swap_url_check = SWAP_URL.split('#')[0]
    is_on_correct_swap_page = page.url.startswith(base_swap_url_check) and page.url.endswith("#SwapBoard")
    if not is_on_correct_swap_page:
        logger.info(f"[{task_name}] Not on target SWAP_URL ({page.url}). Navigating to {SWAP_URL}...")
        try:
            await page.goto(SWAP_URL, timeout=45000, wait_until="domcontentloaded")
            logger.info(f"[{task_name}] Navigated to {page.url}. Let page settle and re-check.")
            await page.wait_for_timeout(1500)
            for frame in page.frames:
                try:
                    if frame.url and frame.url.endswith("/SwapBoard/Index"):
                        await frame.wait_for_selector(ROW_SEL, timeout=3000, state="visible")
                        logger.info(f"✔ [{task_name}] Found iframe by URL suffix '/SwapBoard/Index' AFTER navigation: {frame.url}")
                        return frame
                except Exception: pass
        except Exception as e_nav: logger.error(f"[{task_name}] Error navigating to SWAP_URL: {e_nav}")
    logger.debug(f"[{task_name}] Checking MAIN page for rows (current URL: {page.url})...")
    try:
        await page.wait_for_selector(ROW_SEL, timeout=5000, state="visible")
        logger.info(f"✔ [{task_name}] Grid rows found directly on MAIN page: {page.url}")
        return page
    except PlaywrightTimeoutError:
        logger.debug(f"[{task_name}] No rows ('{ROW_SEL}') found directly on main page {page.url} after waiting. Checking other frames as fallback.")
    except Exception as e:
        logger.debug(f"[{task_name}] Error checking main page for rows: {e}")
    logger.debug(f"[{task_name}] Fallback: Iterating all frames for ROW_SEL presence.")
    for frame in page.frames:
        try:
            await frame.wait_for_selector(ROW_SEL, timeout=2000, state="visible")
            logger.info(f"✔ [{task_name}] Found target iframe by '{ROW_SEL}' presence (fallback): {frame.name or frame.url}")
            return frame
        except Exception: pass
    logger.warning(f"[{task_name}] No grid context conclusively found. Defaulting to main page {page.url}.")
    return page

async def _configure_swap_board_view(page_or_frame, user_email_for_log, configure_period=True, configure_page_size=True): # ... (No changes from your version) ...
    logger.info(f"Configuring grid view for {user_email_for_log} (period: {configure_period}, page_size: {configure_page_size})...")
    if isinstance(page_or_frame, Page): main_page_for_overlays, action_context = page_or_frame, page_or_frame
    else: main_page_for_overlays, action_context = page_or_frame.page, page_or_frame
    async def safe_click_action(target_ctx_for_trigger, sel_trigger, sel_item, desc_trigger, desc_item, is_overlay_item=True, post_item_delay=2000):
        retries = 3
        for attempt in range(retries):
            try:
                logger.info(f"Attempt {attempt + 1}/{retries} to click {desc_trigger} ('{sel_trigger}') on {type(target_ctx_for_trigger)}")
                trigger_el = await target_ctx_for_trigger.wait_for_selector(sel_trigger, timeout=10000, state="visible")
                await trigger_el.click(timeout=7000); logger.info(f"Clicked {desc_trigger}.")
                await main_page_for_overlays.wait_for_timeout(750); break
            except Exception as e:
                logger.warning(f"Attempt {attempt+1} for {desc_trigger} failed: {e}")
                if attempt < retries - 1: await asyncio.sleep(1.0 + attempt * 0.5)
                else: logger.error(f"All attempts for {desc_trigger} failed."); return False
        for attempt in range(retries):
            try:
                item_ctx = main_page_for_overlays if is_overlay_item else target_ctx_for_trigger
                logger.info(f"Attempt {attempt + 1}/{retries} to click {desc_item} ('{sel_item}') on {type(item_ctx)}")
                item_el = await item_ctx.wait_for_selector(sel_item, timeout=10000, state="visible")
                await item_el.click(timeout=7000); logger.info(f"Clicked {desc_item}.")
                await main_page_for_overlays.wait_for_timeout(post_item_delay)
                try: await main_page_for_overlays.wait_for_selector(MASK_SEL, state="detached", timeout=30000); logger.info(f"Mask detached after {desc_item}.")
                except PlaywrightTimeoutError: logger.warning(f"Mask didn't detach post {desc_item}")
                return True
            except Exception as e:
                logger.warning(f"Attempt {attempt+1} for {desc_item} failed: {e}")
                if attempt < retries - 1: await asyncio.sleep(1.0 + attempt * 0.5)
                else: logger.error(f"All attempts for {desc_item} failed."); return False
        return False
    if configure_page_size: logger.info("Setting page size to 'All'..."); await safe_click_action(action_context, PAGESIZE_WRAP_SEL, LI_ALL_SEL, "PageSizeTrigger", "PageSize'All'")
    if configure_period: logger.info("Setting period to 'Month'..."); await safe_click_action(action_context, PERIOD_WRAP_SEL, LI_MON_SEL, "PeriodTrigger", "Period'Month'")
    logger.info(f"Grid view config finished for {user_email_for_log}."); await main_page_for_overlays.wait_for_timeout(1000)

# In tasks.py

# ==================== REPLACE THIS ENTIRE FUNCTION ====================
async def _scroll_full_grid(page_or_frame: Page | Frame, user_email_for_log: str):
    """
    Scrolls the grid context repeatedly until the scroll height of the page
    stabilizes, ensuring all dynamically loaded items are visible.
    """
    eval_context = page_or_frame
    logger.info(f"Scrolling for {user_email_for_log} using ADVANCED BODY SCROLL on {type(eval_context)} (URL: {eval_context.url}).")

    last_h = -1
    no_h_change_count = 0
    max_loops = 30  # Safety break to prevent infinite loops

    for i in range(max_loops):
        try:
            # Get current scroll height BEFORE scrolling
            prev_h = await eval_context.evaluate("document.body.scrollHeight")
            
            # Scroll to the very bottom of the current content
            await eval_context.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            
            # Wait for a moment to allow new content to be requested and loaded
            await eval_context.wait_for_timeout(1500) 
            
            # Get new scroll height AFTER scrolling and waiting
            cur_h = await eval_context.evaluate("document.body.scrollHeight")
            
            logger.debug(f"Scroll loop {i+1}/{max_loops}: prev_h={prev_h}, cur_h={cur_h}")

            # The key logic: if the height hasn't changed after scrolling and waiting,
            # we might be at the end. We wait for 3 consecutive non-changes to be sure.
            if cur_h == prev_h:
                no_h_change_count += 1
                if no_h_change_count >= 3:
                    logger.info("Page height has stabilized after 3 checks. Assuming end of content.")
                    break
            else:
                # If height changed, new content was loaded. Reset the counter.
                no_h_change_count = 0
        except Exception as e:
            logger.error(f"Error during advanced scroll evaluation: {e}")
            break
        
        if i == max_loops - 1:
            logger.warning("Max scroll loops reached. The page might be extremely long or stuck in a loop.")

    logger.info("Advanced scrolling complete.")
    
    # Final check for the loading mask after all scrolling is done
    mask_ctx = eval_context.page if isinstance(eval_context, Frame) else eval_context
    try:
        await mask_ctx.wait_for_selector(MASK_SEL, state="detached", timeout=15000)
        logger.info("Mask detached post-scroll.")
    except Exception:
        logger.warning("Mask not detached post-scroll.")
# =========================================================================


async def _scrape_rows_from_current_view(page_or_frame, user_email, existing_texts, max_r=None): # ... (No changes from your version) ...
    newly_scraped = []; txt_err = "N/A"
    try: rows = await page_or_frame.query_selector_all(ROW_SEL)
    except Exception as e: logger.error(f"Querying rows for {user_email} failed: {e}"); return []
    logger.info(f"Found {len(rows)} potential rows for {user_email}.")
    checked = 0
    for row_el in rows:
        if max_r and checked >= max_r: logger.info(f"Max rows {max_r} checked."); break
        try:
            txt_err = await row_el.inner_text(); norm_txt = norm(txt_err)
            skip = ["AvailabilityIs", "ScheduleWithin", "TotalsWithin", "No data", "Showing items"]
            if not norm_txt or any(s.lower() in norm_txt.lower() for s in skip): continue
            if norm_txt not in existing_texts:
                parsed = parse_shift_details(norm_txt)
                newly_scraped.append(parsed); existing_texts.add(norm_txt)
            checked += 1
        except Exception as e: logger.error(f"Processing row for {user_email} failed: {e}. Text: '{txt_err[:100]}...'")
    logger.info(f"Processed {len(newly_scraped)} new shifts ({checked} rows checked) for {user_email}.")
    return newly_scraped

# --- Celery Tasks (Application Logic) ---
@celery_app.task(name="tasks.send_shift_notification_email", max_retries=3, default_retry_delay=120)
def send_shift_notification_email_task(user_email_to: str, subject: str, body: str, importance: str = "normal"): # ... (No changes from your version) ...
    task_id_log = f"(EmailTask ID: {send_shift_notification_email_task.request.id})" if send_shift_notification_email_task.request.id else ""
    logger.info(f"{task_id_log} Attempting to send email to: {user_email_to}, Subject: {subject[:50]}...")
    if not BOT_SENDER_EMAIL: logger.error(f"{task_id_log} Cannot send email: BOT_SENDER_EMAIL not configured."); return {"status":"error","message":"Bot sender email not configured."}
    access_token = acquire_celery_worker_token()
    if not access_token: msg = f"Failed to acquire MSAL token for {BOT_SENDER_EMAIL}. Email not sent."; logger.error(f"{task_id_log} {msg}"); return {"status":"error","message":msg}
    email_payload = {"message": {"subject":subject,"importance":importance,"body":{"contentType":"Text","content":body},"toRecipients": [{"emailAddress":{"address":user_email_to}}]}}
    try:
        response = requests.post("https://graph.microsoft.com/v1.0/me/sendMail", headers={"Authorization":f"Bearer {access_token}","Content-Type":"application/json"}, json=email_payload, timeout=30)
        if response.status_code == 202: logger.info(f"✔ {task_id_log} Email sent to {user_email_to}. Subject: {subject[:50]}."); return {"status":"success","message":f"Email sent to {user_email_to}"}
        else:
            logger.error(f"❌ {task_id_log} Failed to send email to {user_email_to}. Status: {response.status_code}, Response: {response.text[:300]}")
            if response.status_code in [401,403]: logger.warning(f"{task_id_log} MSAL token invalid/expired or permissions error."); return {"status":"error","message":f"Graph API Auth error {response.status_code}"}
            elif response.status_code in [500,502,503,504]: raise send_shift_notification_email_task.retry(exc=requests.exceptions.HTTPError(f"Graph API server error {response.status_code}"))
            return {"status":"error","message":f"Graph API error {response.status_code}"}
    except requests.exceptions.RequestException as e_req: logger.error(f"❌ {task_id_log} RequestException sending to {user_email_to}: {e_req}"); raise send_shift_notification_email_task.retry(exc=e_req)
    except Exception as e_send: logger.error(f"❌ {task_id_log} Unexpected error sending to {user_email_to}: {e_send}", exc_info=True); return {"status":"error","message":f"Unexpected email error: {str(e_send)}"}

def _perform_interactive_login_sync_wrapper(user_id_for_path, user_email_for_log): # ... (No changes from your version) ...
    async def _actual_playwright_logic_async():
        user_specific_data_dir=os.path.join(USER_DATA_ROOT_DIR,f"user_{user_id_for_path}");os.makedirs(user_specific_data_dir,exist_ok=True);storage_state_file_path=os.path.join(user_specific_data_dir,STORAGE_STATE_FILENAME);login_was_successful=False;browser=None;context=None
        logger.info(f"Attempting Playwright launch for {user_email_for_log}. Storage: {storage_state_file_path}")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'], slow_mo=0)
            context = await browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 960})
            page = await context.new_page()
            try:
                await page.goto(SWAP_URL, timeout=90000, wait_until="domcontentloaded")
                logger.info(f"Nav to {page.url} complete for {user_email_for_log}. Login page should be visible.")
                logger.info(f"▶ Playwright launched for {user_email_for_log}. Please complete login & Duo.")
                await page.wait_for_url(lambda url:"uge.schedulesource.net/TeamWork5" in url, timeout=0)
                logger.info(f"✔ Login & Duo complete for {user_email_for_log}. URL: {page.url}")
                target_for_ops=await _find_target_frame_for_grid(page,task_name="DuoSetup")
                await _configure_swap_board_view(target_for_ops, user_email_for_log, configure_period=True, configure_page_size=True)
                mask_check_ctx=target_for_ops.page if isinstance(target_for_ops,Frame) else target_for_ops
                try: await mask_check_ctx.wait_for_selector(MASK_SEL,state="detached",timeout=30000);logger.info(f"✔ Mask detached post-config for {user_email_for_log}.")
                except Exception: logger.warning(f"Mask not detached post-config for {user_email_for_log}.")
                row_sel_found = bool(await target_for_ops.query_selector(ROW_SEL))
                if not row_sel_found: logger.warning(f"ROW_SEL not immediately found after config for {user_email_for_log}.")
                await context.storage_state(path=storage_state_file_path)
                if os.path.exists(storage_state_file_path) and os.path.getsize(storage_state_file_path)>100:
                    logger.info(f"✔ Session state saved: {storage_state_file_path}");login_was_successful=True
                    alert_msg=f"Login & Duo complete for {user_email_for_log}. Session saved."+("" if row_sel_found else " (Note: Shift table rows not seen).")
                    try: await page.evaluate(f'alert("{alert_msg} Window closing soon.");');await page.wait_for_timeout(7000)
                    except Exception:pass
                else: logger.error(f"Failed to save session state for {user_email_for_log}: {storage_state_file_path}");
            except Exception as e:logger.error(f"Error in Duo Playwright for {user_email_for_log}: {e}",exc_info=True)
            finally:
                logger.info(f"Cleaning Playwright for {user_email_for_log} (DUO)...")
                if context:await context.close()
                if browser:await browser.close()
        return login_was_successful
    loop = None
    try:
        try: loop = asyncio.get_running_loop()
        except RuntimeError: loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        return loop.run_until_complete(_actual_playwright_logic_async())
    finally: logger.info(f"Asyncio wrapper for DUO LOGIN of {user_email_for_log} finished.")

# ==================== ADD THE FOLLOWING CODE BLOCK HERE ====================
# This is the Celery Beat scheduler configuration.
# It tells Celery to run a specific task on a schedule.

@celery_app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    """
    Sets up the periodic tasks for Celery Beat.
    This function is called once, when the Celery app is configured.
    """
    logger.info("--- Setting up periodic tasks for all active users ---")
    
    # We want to use the main 'quick_check_shifts_for_user' task as our periodic check.
    # It's more efficient than the full scrape.
    # We will schedule it to run every 90 seconds. You can change this value.
    sender.add_periodic_task(
        90.0,  # The interval in seconds. e.g., 90.0 for every 90 seconds.
        trigger_all_active_user_checks.s(), # The task to run (we will define this next)
        name='trigger shift checks for all active users'
    )

@celery_app.task
def trigger_all_active_user_checks():
    """
    This is a "meta-task" that finds all active users and creates
    an individual check task for each one.
    """
    db_session = SessionLocal()
    try:
        active_users = db_session.query(User).filter_by(monitoring_active=True, duo_done=True).all()
        if not active_users:
            logger.info("PERIODIC_TRIGGER: No active users to check.")
            return

        logger.info(f"PERIODIC_TRIGGER: Found {len(active_users)} active users. Queueing individual checks.")
        for user in active_users:
            # For each active user, we queue their individual quick_check task.
            quick_check_shifts_for_user.delay(user.id)
            logger.info(f"  - Queued quick_check for {user.email} (ID: {user.id})")
            
    except Exception as e:
        logger.error(f"PERIODIC_TRIGGER: Error querying for active users: {e}", exc_info=True)
    finally:
        db_session.close()

# ==================== END OF THE NEW CODE BLOCK ====================

@celery_app.task(name="tasks.open_login_browser_for_duo_setup", bind=True)
def open_login_browser_for_duo_setup(self, user_id: int): # ... (No changes from your latest version, with email call) ...
    task_id_log = f"(ID: {self.request.id})" if self.request.id else ""
    logger.info(f"\n--- Task 'open_login_browser_for_duo_setup' {task_id_log} received for user_id: {user_id} ---")
    db_session = SessionLocal();user = db_session.query(User).get(user_id)
    if not user: logger.error(f"User {user_id} not found."); db_session.close(); return {"status":"error","message":"User not found"}
    logger.info(f"Proceeding with Duo setup for {user.email}.")
    try:
        login_successful = _perform_interactive_login_sync_wrapper(user.id, user.email)
        user_to_update = db_session.query(User).get(user_id)
        if not user_to_update: logger.error(f"User {user_id} disappeared."); db_session.rollback(); return {"status":"error","message":"User vanished."}
        user_to_update.duo_done = login_successful
        if login_successful: user_to_update.monitoring_active = False; logger.info(f"Monitoring_active set to False for {user_to_update.email} post-Duo.")
        db_session.add(user_to_update); db_session.commit()
        msg = "Duo setup successful and ScheduleSource session saved." if login_successful else "Duo setup process was not completed successfully."
        status = "success" if login_successful else "warning"
        logger.info(f"{status.capitalize()} {task_id_log}: {msg} for {user_to_update.email}. duo_done: {login_successful}")
        if login_successful:
            email_subject = "ShiftBot Portal: Duo & ScheduleSource Setup Successful"
            email_body = (f"Hello {user_to_update.email.split('@')[0] if user_to_update.email else 'User'},\n\n"
                          f"Your interactive login and Duo setup for ScheduleSource via the ShiftBot Portal was successful.\nYour session has been saved.\n\n"
                          f"You can now start shift monitoring or perform full shift scrapes from the portal.\n\nThanks,\nThe ShiftBot Team")
            send_shift_notification_email_task.delay(user_to_update.email, email_subject, email_body)
            logger.info(f"Queued Duo success confirmation email to {user_to_update.email}")
        return {"status":status,"message":msg}
    except Exception as e:
        db_session.rollback();logger.critical(f"CRITICAL Error for {getattr(user,'email','N/A')} in task {task_id_log}: {e}",exc_info=True)
        try:
            u=db_session.query(User).get(user_id)
            if u:u.duo_done=False;u.monitoring_active=False;db_session.add(u);db_session.commit()
        except Exception as e_db:logger.error(f"Failed to update user status on critical error: {e_db}")
        return {"status":"error","message":f"Task error: {str(e)}"}
    finally: db_session.close();logger.info(f"--- Task {task_id_log} finished for user_id: {user_id} --- \n")

def parse_shift_details(raw_shift_text: str) -> dict: # ... (No changes from your version) ...
    details={"raw_text":raw_shift_text,"shift_date_str":"N/A","shift_time_str":"N/A","shift_position_str":"N/A"}
    if not raw_shift_text:return details
    norm_txt=norm(raw_shift_text);date_m=re.search(r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})",norm_txt)

    if date_m:details["shift_date_str"]=date_m.group(1)
    time_m=re.findall(r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))",norm_txt)
    if len(time_m)>=2:details["shift_time_str"]=f"{time_m[0]} - {time_m[1]}"
    elif len(time_m)==1:details["shift_time_str"]=time_m[0]
    known_pos=[ "Passenger Assistance Agent New Terminal A", # Keep full for matching
        "Vendor Behind Counter New Terminal A",

        # Keep full for matching
        "Passenger Assistance Agent", # More general, should be checked after NTA versions
        "Vendor Behind Counter",    # More general
        "Hub Staging Agent",
        "Call Forwarding",
        "Queue Management",
        "As Assigned",
        "Bag Drop Term C", # New
        "Bag Drop Term A"  # New
        # If "passenger assistant NTA" can appear as just "NTA" in some contexts and means PAA@NTA,
        # you might need more complex regex or keyword spotting if the string isn't exact.
        # For now, this list expects these full strings to be present for a match.
         ]
    txt_low=norm_txt.lower();found_pos="N/A"
    for pos in known_pos:
        if pos.lower() in txt_low:found_pos=pos;break
    details["shift_position_str"]=found_pos;return details


def _quick_check_shifts_sync_wrapper(user_id_for_path, user_email_for_log): # ... (No changes from your version) ...
    async def _actual_quick_check_logic_async():
        logger.info(f"Starting QUICK CHECK (Current & Next Month) for {user_email_for_log} (User ID: {user_id_for_path})")
        user_specific_data_dir=os.path.join(USER_DATA_ROOT_DIR,f"user_{user_id_for_path}");storage_state_file_path=os.path.join(user_specific_data_dir,STORAGE_STATE_FILENAME)
        if not os.path.exists(storage_state_file_path) or os.path.getsize(storage_state_file_path)<100:raise PlaywrightError(f"QuickCheck: Storage state missing for {user_email_for_log}")
        newly_found_for_notification = []; raw_texts_scraped_this_run = set(); all_shifts_this_run = []
        browser = None; context = None
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True, slow_mo=0)
                context = await browser.new_context(storage_state=storage_state_file_path)
                page = await context.new_page()
                await page.goto(SWAP_URL, timeout=35000, wait_until="domcontentloaded")
                logger.info(f"QuickCheck: Navigated to {page.url} for {user_email_for_log}")
                if "signon.ual.com" in page.url.lower() or "login" in page.url.lower(): raise PlaywrightError(f"Session expired for {user_email_for_log}, on login page.")
                current_month_target = await _find_target_frame_for_grid(page, "QuickCheckCurrent")
                await _configure_swap_board_view(current_month_target, user_email_for_log, configure_period=True, configure_page_size=True)
                if await current_month_target.query_selector(ROW_SEL):
                    await _scroll_full_grid(current_month_target, user_email_for_log)
                    shifts = await _scrape_rows_from_current_view(current_month_target, user_email_for_log, raw_texts_scraped_this_run, QUICK_CHECK_MAX_ROWS)
                    all_shifts_this_run.extend(shifts); logger.info(f"QuickCheck: Scraped {len(shifts)} from current month.")
                else: logger.info("QuickCheck: No rows in current month.")
                next_btn = await current_month_target.query_selector(f"{NEXT_PERIOD_BUTTON_SEL}:not([disabled])")
                if next_btn:
                    await next_btn.click(timeout=10000)
                    mask_ctx = current_month_target.page if isinstance(current_month_target,Frame) else current_month_target
                    await mask_ctx.wait_for_timeout(2000)
                    try: await mask_ctx.wait_for_selector(MASK_SEL, state="detached", timeout=25000); logger.info("QuickCheck: Mask detached for next month.")
                    except PlaywrightTimeoutError: logger.warning("QuickCheck: Mask not detached for next month.")
                    next_month_target = await _find_target_frame_for_grid(page, "QuickCheckNext")
                    if await next_month_target.query_selector(ROW_SEL):
                        await _scroll_full_grid(next_month_target, user_email_for_log)
                        shifts = await _scrape_rows_from_current_view(next_month_target, user_email_for_log, raw_texts_scraped_this_run, QUICK_CHECK_MAX_ROWS)
                        all_shifts_this_run.extend(shifts); logger.info(f"QuickCheck: Scraped {len(shifts)} from next month.")
                    else: logger.info("QuickCheck: No rows in next month.")
                else: logger.info("QuickCheck: Next Period button not available.")
                logger.info(f"QuickCheck: Total {len(all_shifts_this_run)} shifts before Redis compare for {user_email_for_log}.")
                if redis_client:
                    cache_key=f"{RECENTLY_SEEN_CACHE_PREFIX}{user_id_for_path}";from_redis=redis_client.smembers(cache_key);to_cache=set()
                    for s_detail in all_shifts_this_run:
                        if s_detail["raw_text"] not in from_redis: newly_found_for_notification.append(s_detail);to_cache.add(s_detail["raw_text"])
                    if to_cache:
                        pipe=redis_client.pipeline();pipe.sadd(cache_key,*to_cache);pipe.expire(cache_key,RECENTLY_SEEN_CACHE_EXPIRY_SECONDS);pipe.execute()
                        logger.info(f"QuickCheck: Found {len(newly_found_for_notification)} new shifts (vs Redis). Updated Redis for {user_email_for_log}.")
                    elif from_redis and all_shifts_this_run: redis_client.expire(cache_key,RECENTLY_SEEN_CACHE_EXPIRY_SECONDS);logger.info(f"QuickCheck: No new (vs Redis) shifts for {user_email_for_log}. Cache expiry extended.")
                    elif not all_shifts_this_run: logger.info(f"QuickCheck: No shifts found this run for {user_email_for_log}.");
                    else:
                        to_prime_cache={s["raw_text"] for s in all_shifts_this_run}
                        if to_prime_cache: pipe=redis_client.pipeline();pipe.sadd(cache_key,*to_prime_cache);pipe.expire(cache_key,RECENTLY_SEEN_CACHE_EXPIRY_SECONDS);pipe.execute();logger.info(f"QuickCheck: {len(newly_found_for_notification)} shifts found, Redis was empty. Primed cache for {user_email_for_log}.")
                else: logger.warning("QuickCheck: Redis unavailable."); newly_found_for_notification.extend(all_shifts_this_run)
            except PlaywrightError as pe: logger.error(f"QuickCheck PlaywrightError for {user_email_for_log}: {pe}"); raise
            except Exception as e: logger.error(f"QuickCheck Error for {user_email_for_log}: {e}",exc_info=True); raise
            finally:
                if context: await context.close()
                if browser: await browser.close()
                logger.info(f"QuickCheck: Playwright closed for {user_email_for_log}.")
        return newly_found_for_notification
    loop = None
    try:
        try: loop = asyncio.get_running_loop()
        except RuntimeError: loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        return loop.run_until_complete(_actual_quick_check_logic_async())
    finally: logger.info(f"Asyncio wrapper for QUICK CHECK of {user_email_for_log} finishing.")

@celery_app.task(name="tasks.quick_check_shifts_for_user", bind=True, max_retries=2, default_retry_delay=45)
def quick_check_shifts_for_user(self, user_id: int): # ... (No changes from your version, assumes check_shift_against_user_preferences handles new pref structure) ...
    task_id_log = f"(ID: {self.request.id})" if self.request.id else ""; logger.info(f"--- Task 'quick_check_shifts_for_user' {task_id_log} for user_id: {user_id} ---")
    db_session = SessionLocal(); user = db_session.query(User).get(user_id)
    can_run_reschedule = user and user.monitoring_active and user.duo_done
    if not user: logger.warning(f"QC User {user_id} not found.")
    elif not user.duo_done: logger.warning(f"QC Duo not done for {user.email}.")
    elif not user.monitoring_active: logger.info(f"QC Monitoring inactive for {user.email}.")
    if not can_run_reschedule: db_session.close(); logger.info(f"QC task {task_id_log} for {user_id} will NOT run/reschedule."); return
    session_expired = False
    try:
        # ==================== NEW LINE TO ADD ====================
        # Before starting a new check, reset the 'is_new' flag for all of the user's old shifts.
        db_session.query(ScrapedShift).filter_by(user_id=user.id, is_new_for_user=True).update({"is_new_for_user": False})
        db_session.commit() # Commit this change immediately
        # =========================================================

        shifts_new_vs_redis = _quick_check_shifts_sync_wrapper(user.id, user.email)
        if shifts_new_vs_redis:
            logger.info(f"QC for {user.email} ID'd {len(shifts_new_vs_redis)} shifts as new vs Redis.")
            prefs = db_session.query(ShiftConfig).filter_by(user_id=user.id).all()
            if not prefs: logger.info(f"User {user.email} has no prefs; all new-to-cache are candidates.")
            notified_count = 0
            for shift_details in shifts_new_vs_redis:
            # This part is correct, it checks if the shift matches any of your saved preferences
                matches_prefs = check_shift_against_user_preferences(shift_details, prefs)
            
            existing_db_shift = db_session.query(ScrapedShift).filter_by(user_id=user.id, raw_text=shift_details["raw_text"]).first()
            
            send_email_now = False
            
            if not existing_db_shift:
                # --- THIS IS THE CRITICAL FIX ---
                # A shift is only "new for the user" if it ALSO matches their preferences.
                # If they have no preferences, nothing should be marked as new.
                is_truly_new = matches_prefs and bool(prefs)
                
                new_shift = ScrapedShift(
                    user_id=user.id,
                    raw_text=shift_details["raw_text"],
                    shift_date_str=shift_details["shift_date_str"],
                    shift_time_str=shift_details["shift_time_str"],
                    shift_position_str=shift_details["shift_position_str"],
                    is_new_for_user=is_truly_new,  # Use our new flag here
                    scraped_at=datetime_utc()
                )
                db_session.add(new_shift)
                logger.info(f"NEWLY ADDED TO DB (QC {user.email}, matches={matches_prefs}, is_new={is_truly_new}): {shift_details['raw_text'][:40]}...")
                
                if is_truly_new:
                    send_email_now = True
                else:
                    if matches_prefs and not existing_db_shift.is_new_for_user: existing_db_shift.is_new_for_user = True; logger.info(f"RE-FLAGGING (QC {user.email}): {existing_db_shift.raw_text[:40]}..."); send_email_now = True
                    elif not matches_prefs and existing_db_shift.is_new_for_user: existing_db_shift.is_new_for_user = False; logger.info(f"UNFLAGGING (QC {user.email}): {existing_db_shift.raw_text[:40]}...")
                    existing_db_shift.scraped_at = datetime_utc(); db_session.add(existing_db_shift)
                if send_email_now:
                    notified_count += 1; subject = f"Shift Alert: {shift_details.get('shift_position_str', 'Shift')}"
                    body = f"Hello {user.email.split('@')[0] if user.email else 'User'},\nA new shift is available:\n\n{shift_details['raw_text']}\n\nDate: {shift_details.get('shift_date_str')}\nTime: {shift_details.get('shift_time_str')}\nPosition: {shift_details.get('shift_position_str')}\n\nShiftBot"
                    send_shift_notification_email_task.delay(user.email, subject, body)
            if notified_count > 0 or (not prefs and shifts_new_vs_redis): db_session.commit(); logger.info(f"QC DB commit for {user.email}. {notified_count} email(s) queued.")
            elif shifts_new_vs_redis: db_session.commit(); logger.info(f"QC DB commit for {user.email} (scraped_at updates). No new pref matches.")
    except PlaywrightError as pe:
        logger.error(f"QC PlaywrightError for {user.email}: {pe}")
        if "Session expired" in str(pe) or "missing" in str(pe).lower():
            logger.warning(f"SESSION EXPIRED/INVALID for {user.email}. Disabling monitoring.")
            if user: user.duo_done=False;user.monitoring_active=False;db_session.add(user);db_session.commit()
            session_expired = True
        else:
            try: self.retry(exc=pe, countdown=60)
            except Exception as e_retry: logger.error(f"Failed to retry QC task: {e_retry}")
        can_run_reschedule = False
    except Exception as e:
        logger.error(f"QC General error for {getattr(user, 'email', 'N/A')}: {e}", exc_info=True)
        can_run_reschedule = False # Stop rescheduling on general error
    finally:
        # This block MUST use a new session to check the user's status,
        # because the 'db_session' from the try block is now closed.
        db_session_finally = SessionLocal()
        try:
            # Re-fetch the user from the database in the new session.
            user_for_reschedule_check = db_session_finally.get(User, user_id)
            if user_for_reschedule_check and user_for_reschedule_check.monitoring_active and not session_expired:
                logger.info(f"Rescheduling QC for {user_for_reschedule_check.email} in {QUICK_CHECK_INTERVAL_SECONDS}s.")
                quick_check_shifts_for_user.apply_async(args=[user_id], countdown=QUICK_CHECK_INTERVAL_SECONDS)
            else:
                user_email_log = getattr(user_for_reschedule_check, 'email', f'ID {user_id}')
                monitoring_status = getattr(user_for_reschedule_check, 'monitoring_active', 'N/A')
                logger.info(f"NOT rescheduling QC for user {user_email_log} (active: {monitoring_status}, session_exp: {session_expired}).")
        except Exception as e_finally:
            logger.error(f"Error in 'finally' block for QC task user {user_id}: {e_finally}")
        finally:
            # IMPORTANT: Close the session we created for this finally block.
            db_session_finally.close()

        logger.info(f"--- QC task {task_id_log} finished for user_id: {user_id} ---")

def _scrape_shifts_sync_wrapper(user_id_for_path, user_email_for_log): # ... (No changes from your version) ...
    async def _actual_scrape_logic_async():
        user_sdd=os.path.join(USER_DATA_ROOT_DIR,f"user_{user_id_for_path}");ssf_path=os.path.join(user_sdd,STORAGE_STATE_FILENAME)
        if not os.path.exists(ssf_path) or os.path.getsize(ssf_path)<100:raise PlaywrightError(f"FullScrape: Storage state missing for {user_email_for_log}")
        logger.info(f"FULL SCRAPE for {user_email_for_log} using storage: {ssf_path}")
        all_scraped_dicts=[]; unique_raws_this_run=set(); browser=None; context=None
        async with async_playwright() as p:
            try:
                browser=await p.chromium.launch(headless=True,slow_mo=0)
                context=await browser.new_context(storage_state=ssf_path)
                page=await context.new_page();await page.set_viewport_size({"width":1366,"height":768})
                await page.goto(SWAP_URL,timeout=45000,wait_until="domcontentloaded")
                logger.info(f"FullScrape: Nav to {page.url} for {user_email_for_log}")
                if "signon.ual.com" in page.url.lower() or "login" in page.url.lower(): raise PlaywrightError(f"Session expired for {user_email_for_log}, on login page.")
                current_target = await _find_target_frame_for_grid(page, "FullScrapeCurrent")
                await _configure_swap_board_view(current_target, user_email_for_log, configure_period=False, configure_page_size=True)
                if await current_target.query_selector(ROW_SEL):
                    await current_target.wait_for_selector(ROW_SEL,timeout=30000,state="visible");logger.info("FullScrape: Table for current month.")
                    await _scroll_full_grid(current_target,user_email_for_log)
                    shifts=await _scrape_rows_from_current_view(current_target,user_email_for_log,unique_raws_this_run)
                    all_scraped_dicts.extend(shifts);logger.info(f"FullScrape: {len(shifts)} from current month.")
                else: logger.info("FullScrape: No rows in current month.")
                next_btn = await current_target.query_selector(f"{NEXT_PERIOD_BUTTON_SEL}:not([disabled])")
                if next_btn:
                    await next_btn.click(timeout=10000)
                    mask_ctx=current_target.page if isinstance(current_target,Frame) else current_target
                    await mask_ctx.wait_for_timeout(2000)
                    try: await mask_ctx.wait_for_selector(MASK_SEL,state="detached",timeout=25000);logger.info("FullScrape: Mask detached next month.")
                    except PlaywrightTimeoutError: logger.warning("FullScrape: Mask not detached next month.")
                    next_target = await _find_target_frame_for_grid(page, "FullScrapeNext")
                    if await next_target.query_selector(ROW_SEL):
                        await next_target.wait_for_selector(ROW_SEL,timeout=30000,state="visible");logger.info("FullScrape: Table for next month.")
                        await _scroll_full_grid(next_target,user_email_for_log)
                        shifts=await _scrape_rows_from_current_view(next_target,user_email_for_log,unique_raws_this_run)
                        all_scraped_dicts.extend(shifts);logger.info(f"FullScrape: {len(shifts)} from next month.")
                    else: logger.info("FullScrape: No rows in next month.")
                else: logger.info("FullScrape: Next Period btn not available.")
            except PlaywrightError as pe: logger.error(f"FullScrape PlaywrightError for {user_email_for_log}: {pe}"); raise
            except Exception as e: logger.error(f"FullScrape Error for {user_email_for_log}: {e}",exc_info=True); raise
            finally:
                if context: await context.close()
                if browser: await browser.close()
                logger.info(f"FullScrape: Playwright closed for {user_email_for_log}.")
        logger.info(f"FULL SCRAPE for {user_email_for_log} got {len(all_scraped_dicts)} unique shifts.")
        return all_scraped_dicts
    loop = None
    try:
        try: loop = asyncio.get_running_loop()
        except RuntimeError: loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        return loop.run_until_complete(_actual_scrape_logic_async())
    finally: logger.info(f"Asyncio wrapper for FULL SCRAPE of {user_email_for_log} finishing.")

@celery_app.task(name="tasks.trigger_shift_scrape_for_user", bind=True, max_retries=1, default_retry_delay=120)
def trigger_shift_scrape_for_user(self, user_id: int): # ... (No changes from your version) ...
    task_id_log=f"(ID: {self.request.id})" if self.request.id else ""; logger.info(f"\n--- Task 'trigger_shift_scrape_for_user' (FS) {task_id_log} for user_id: {user_id} ---")
    db_session=SessionLocal(); user=db_session.query(User).get(user_id)
    if not user: logger.error(f"FS User {user_id} not found."); db_session.close(); return {"status":"error","message":"User not found"}
    ssf_path=os.path.join(USER_DATA_ROOT_DIR,f"user_{user.id}",STORAGE_STATE_FILENAME)
    if not user.duo_done or not os.path.exists(ssf_path) or os.path.getsize(ssf_path)<100:
        logger.warning(f"FS Duo/session invalid for {user.email}. Duo: {user.duo_done}, Storage: {os.path.exists(ssf_path)}")
        if user.duo_done: user.duo_done=False;user.monitoring_active=False;db_session.add(user);db_session.commit();logger.info(f"Reset Duo for {user.email} due to bad session.")
        db_session.close(); return {"status":"error","message":"Duo & session file required."}
    try:
        scraped_shifts = _scrape_shifts_sync_wrapper(user.id,user.email)
        if redis_client:
            cache_key=f"{RECENTLY_SEEN_CACHE_PREFIX}{user.id}";raw_texts=[s["raw_text"] for s in scraped_shifts]
            if raw_texts: pipe=redis_client.pipeline();pipe.delete(cache_key);pipe.sadd(cache_key,*raw_texts);pipe.expire(cache_key,RECENTLY_SEEN_CACHE_EXPIRY_SECONDS);pipe.execute();logger.info(f"FS Redis cache primed for {user.email} with {len(raw_texts)} shifts.")
            else: redis_client.delete(cache_key);logger.info(f"FS No shifts, cleared Redis for {user.email}.")
        deleted_count=db_session.query(ScrapedShift).filter_by(user_id=user.id).delete();db_session.commit()
        logger.info(f"FS Deleted {deleted_count} old shifts for {user.email}.")
        added_count=0
        if scraped_shifts:
            for s_detail in scraped_shifts:
                db_shift=ScrapedShift(user_id=user.id,raw_text=s_detail["raw_text"],shift_date_str=s_detail["shift_date_str"],shift_time_str=s_detail["shift_time_str"],shift_position_str=s_detail["shift_position_str"],is_new_for_user=False,scraped_at=datetime_utc())
                db_session.add(db_shift);added_count+=1
            db_session.commit();logger.info(f"FS Saved {added_count} shifts for {user.email} (marked not 'new').")
            return {"status":"success","message":f"Scraped {added_count} shifts (FS).","count":added_count}
        else: logger.info(f"FS No shifts found for {user.email}."); return {"status":"success","message":"No shifts found (FS).","count":0}
    except PlaywrightError as pe:
        logger.error(f"FS PlaywrightError for {user.email}: {pe}")
        if "Session expired" in str(pe) or "missing" in str(pe).lower():
            if user:user.duo_done=False;user.monitoring_active=False;db_session.add(user);db_session.commit();logger.warning(f"SESSION EXPIRED/INVALID for {user.email} (FS). Disabled monitoring.")
            return {"status":"error","message":"Session expired. Re-run Duo."}
        else:
            try:self.retry(exc=pe,countdown=120)
            except Exception as e_retry:logger.error(f"Failed to retry FS task: {e_retry}")
            return {"status":"error","message":f"FS Playwright error: {pe}"}
    except Exception as e: db_session.rollback();logger.error(f"FS General error for {user.email}: {e}",exc_info=True); return {"status":"error","message":f"FS task error: {str(e)}"}
    finally: db_session.close();logger.info(f"--- FS Task {task_id_log} finished for user_id: {user_id} --- \n")

# tasks.py - check_shift_against_user_preferences function
# tasks.py - check_shift_against_user_preferences function

def check_shift_against_user_preferences(scraped_shift_details: dict, user_configs: list) -> bool:
    if not user_configs: return True

    s_date_str = scraped_shift_details.get("shift_date_str", "")
    s_time_str = scraped_shift_details.get("shift_time_str", "").lower() # Scraped shift time
    s_position_str = scraped_shift_details.get("shift_position_str", "").lower() # Scraped shift position

    for config in user_configs:
        date_match_for_this_config = False
        time_match_for_this_config = False
        position_match_for_this_config = False

        active_criteria_in_this_config = 0

        # Date Check (Handles comma-separated dates)
        if config.target_date:
            active_criteria_in_this_config += 1
            if s_date_str:
                preferred_dates_set = {d.strip() for d in config.target_date.split(',')}
                scraped_date_base = s_date_str.split(" ")[0]
                if scraped_date_base in preferred_dates_set:
                    date_match_for_this_config = True
        else:
            date_match_for_this_config = True

        # Time Check (Handles comma-separated times)
        if config.target_time:
            active_criteria_in_this_config += 1
            if s_time_str:
                # Preferred times are stored like "05:00 AM, 01:00 PM"
                preferred_times_list = [t.strip().lower() for t in config.target_time.split(',')]
                for pref_time in preferred_times_list:
                    if pref_time in s_time_str: # Check if a preferred start time is in the scraped time range
                        time_match_for_this_config = True
                        break # One match is enough for this criterion
        else:
            time_match_for_this_config = True

        # Position Check (Handles comma-separated positions)
        if config.target_position:
            active_criteria_in_this_config += 1
            if s_position_str:
                # Preferred positions are stored like "Passenger Assistance Agent, Vendor Behind Counter"
                preferred_positions_list = [p.strip().lower() for p in config.target_position.split(',')]
                for pref_pos in preferred_positions_list:
                    if pref_pos in s_position_str: # Check if a preferred position is in the scraped position
                        position_match_for_this_config = True
                        break # One match is enough
        else:
            position_match_for_this_config = True

        if active_criteria_in_this_config == 0:
            continue

        if date_match_for_this_config and \
           time_match_for_this_config and \
           position_match_for_this_config:
           return True
    return False

# --- SYNC WRAPPER FOR SESSION CHECK ---
def _check_session_validity_sync_wrapper(user_id_for_path, user_email_for_log):
    async def _actual_session_check_logic_async():
        logger.info(f"SESSION_CHECK for {user_email_for_log} (User ID: {user_id_for_path})")
        user_specific_data_dir = os.path.join(USER_DATA_ROOT_DIR, f"user_{user_id_for_path}")
        storage_state_file_path = os.path.join(user_specific_data_dir, STORAGE_STATE_FILENAME)

        if not os.path.exists(storage_state_file_path) or os.path.getsize(storage_state_file_path) < 100:
            logger.error(f"SESSION_CHECK: Storage state missing/invalid for {user_email_for_log}. Cannot check.")
            return {"user_id": user_id_for_path, "status": "error", "reason": "storage_state_missing"}

        is_valid = False
        final_url = "N/A"
        error_message = None
        browser = None
        context = None

        async with async_playwright() as p:
                        # ... inside _actual_session_check_logic_async()

            try:
                # First, launch the browser HEADLESS again. We know what we need to do now.
                browser = await p.chromium.launch(headless=True, slow_mo=0)
                context = await browser.new_context(storage_state=storage_state_file_path)
                page = await context.new_page()

                logger.info(f"SESSION_CHECK: Navigating to SWAP_URL ({SWAP_URL}) for {user_email_for_log}")
                await page.goto(SWAP_URL, timeout=30000, wait_until="domcontentloaded")
                final_url = page.url
                logger.info(f"SESSION_CHECK: Landed on URL: {final_url} for {user_email_for_log}")

                if "signon.ual.com" in final_url.lower() or "login" in final_url.lower():
                    is_valid = False
                    logger.warning(f"SESSION_CHECK: Session for {user_email_for_log} appears EXPIRED. Redirected.")
                else:
                    # Session seems okay, now we perform the FULL validation
                    target_ops = await _find_target_frame_for_grid(page, task_name="SessionCheck")

                    # *** THE CRUCIAL FIX ***
                    # We MUST configure the view to 'Month' to ensure we see all potential rows.
                    await _configure_swap_board_view(
                        target_ops,
                        user_email_for_log,
                        configure_period=True, # Explicitly set Period to 'Month'
                        configure_page_size=True # Also set Page Size to 'All'
                    )

                    # Now, with the view configured, we can reliably check for the rows.
                    try:
                        await target_ops.wait_for_selector(ROW_SEL, timeout=10000, state="visible")
                        is_valid = True
                        logger.info(f"SESSION_CHECK: Session for {user_email_for_log} VALID. Found rows after configuring view.")
                    except PlaywrightTimeoutError:
                        # This means that even in Month view, there are ZERO shifts available.
                        # For the purpose of a session check, this is still a VALID state.
                        # The page is loaded, interactive, and showing an empty grid.
                        is_valid = True
                        logger.warning(f"SESSION_CHECK: Session VALID, but no shifts found in 'Month' view for {user_email_for_log}.")

            except PlaywrightError as pe:
                # ... (rest of the function is the same, no changes to error handling)
                error_message = str(pe)
                is_valid = False # Assume invalid on Playwright error during check
            except Exception as e:
                logger.error(f"SESSION_CHECK: Unexpected error for {user_email_for_log}: {e}", exc_info=True)
                error_message = str(e)
                is_valid = False # Assume invalid on general error
            finally:
                if context: await context.close()
                if browser: await browser.close()
                logger.info(f"SESSION_CHECK: Playwright closed for {user_email_for_log}.")

        return {"user_id": user_id_for_path, "timestamp": datetime_utc().isoformat(), "is_valid": is_valid, "final_url": final_url, "error": error_message}

    # Sync wrapper part
    loop = None
    try:
        try: loop = asyncio.get_running_loop()
        except RuntimeError: loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        return loop.run_until_complete(_actual_session_check_logic_async())
    finally:
        logger.info(f"SESSION_CHECK: Asyncio loop wrapper for {user_email_for_log} finishing.")


@celery_app.task(name="tasks.check_schedulesource_session")
def check_schedulesource_session_task(user_id: int):
    db_session = SessionLocal()
    user = db_session.query(User).get(user_id)
    db_session.close() # Close session as we only needed user email

    if not user:
        logger.error(f"SESSION_CHECK_TASK: User {user_id} not found.")
        return {"user_id": user_id, "status": "error", "reason": "user_not_found"}
    if not user.duo_done:
        logger.warning(f"SESSION_CHECK_TASK: Duo not done for {user.email}. Skipping session check.")
        return {"user_id": user_id, "status": "skipped", "reason": "duo_not_done"}

    result = _check_session_validity_sync_wrapper(user.id, user.email)

    # Log result prominently
    logger.info(f"SESSION_CHECK_RESULT for User ID {result.get('user_id')} ({user.email}): Valid={result.get('is_valid')}, URL={result.get('final_url')}, Error={result.get('error')}")

    # Here you could also update a field on the User model like 'last_session_check_valid'
    # Or if !is_valid, you could set duo_done = False (but be careful not to do this too eagerly)

    return result # Celery task returns the result dict