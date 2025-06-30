# app.py  ───────────────────────────────────────────────────────────────
import os, re, json, secrets, datetime as dt, sqlite3
from flask import (
    Flask, render_template, redirect, url_for, flash,
    request, jsonify, abort
)
from flask_login import (
    LoginManager, login_user, login_required,
    logout_user, current_user
)
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, SubmitField, SelectMultipleField
from wtforms.validators import DataRequired, Email, Optional
from dotenv import load_dotenv

from models import db, User, ScrapedShift, ShiftConfig

# ────────────────────────────────────────────────────────────────────
# Constants from tasks.py (if Celery isn’t installed, fall back)
# ────────────────────────────────────────────────────────────────────

load_dotenv()
app = Flask(__name__)


try:
    from tasks import (
        USER_DATA_ROOT_DIR,
        STORAGE_STATE_FILENAME,
        QUICK_CHECK_INTERVAL_SECONDS as TASK_QUICK_CHECK_INTERVAL,
        quick_check_shifts_for_user 
    )
    CELERY_AVAILABLE = True
except ImportError:
    USER_DATA_ROOT_DIR = "userdata_celery"
    STORAGE_STATE_FILENAME = "storage_state.json"
    TASK_QUICK_CHECK_INTERVAL = "15 (default)"
    quick_check_shifts_for_user = None
    CELERY_AVAILABLE = False
    # Now this line will work correctly
    app.logger.warning("Celery tasks could not be imported. Monitoring will not start.")


# ────────────────────────────────────────────────────────────────────
# Flask & SQLAlchemy setup
# ────────────────────────────────────────────────────────────────────


app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("SQLALCHEMY_DATABASE_URI")
if not app.config["SQLALCHEMY_DATABASE_URI"]:
    raise RuntimeError("SQLALCHEMY_DATABASE_URI missing.")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
lm = LoginManager(app)
lm.login_view = "login"

# ────────────────────────────────────────────────────────────────────
# WTForms (web UI)
# ────────────────────────────────────────────────────────────────────
class RegisterForm(FlaskForm):
    email = StringField("Company Email",
                        validators=[DataRequired(), Email()])
    submit = SubmitField("Register")

class LoginForm(FlaskForm):
    email = StringField("Company Email",
                        validators=[DataRequired(), Email()])
    submit = SubmitField("Log In")

class PrefForm(FlaskForm):
    target_date = StringField(
        "Date(s) (Optional, e.g. mm/dd/yyyy)",
        render_kw={"placeholder": "e.g., 06/20/2025, 07/15/2025"},
        validators=[Optional()],
    )
    PREDEFINED_TIMES = [
        ("03:30 AM", "3:30 AM"), ("04:00 AM", "4:00 AM"),
        ("05:00 AM", "5:00 AM"), ("06:00 AM", "6:00 AM"),
        ("09:00 AM", "9:00 AM"), ("12:00 PM", "12:00 PM"),
        ("01:00 PM", "1:00 PM"), ("02:00 PM", "2:00 PM"),
        ("03:00 PM", "3:00 PM"), ("07:00 PM", "7:00 PM"),
        ("09:00 PM", "9:00 PM"),
    ]
    target_time = SelectMultipleField(
        "Start Time(s) (Optional)",
        choices=PREDEFINED_TIMES,
        validators=[Optional()],
    )
    POSITION_CHOICES = [
        ("", "-- Any Position --"),
        ("Passenger Assistance Agent New Terminal A",
         "Passenger Assistance Agent NTA"),
        ("Vendor Behind Counter New Terminal A",
         "Vendor Behind Counter NTA"),
        ("Passenger Assistance Agent",
         "Passenger Assistance Agent (General)"),
        ("Vendor Behind Counter",
         "Vendor Behind Counter (General)"),
        ("Hub Staging Agent", "Hub Staging Agent"),
        ("Call Forwarding",   "Call Forwarding"),
        ("Queue Management",  "Queue Management"),
        ("As Assigned",       "As Assigned"),
        ("Bag Drop Term C",   "Bag Drop Term C"),
        ("Bag Drop Term A",   "Bag Drop Term A"),
    ]
    target_position = SelectMultipleField(
        "Position(s) (Optional)",
        choices=POSITION_CHOICES,
        validators=[Optional()],
    )
    submit = SubmitField("Save This Preference")

# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────
@lm.user_loader
def load_user(user_id):          # for flask-login
    return db.session.get(User, int(user_id))

def _auth_from_header():
    """Mobile-API helper – returns `User` or aborts 401/403."""
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "):
        abort(401, description="Missing Bearer token.")
    api_key = hdr.split(" ", 1)[1]
    user = User.query.filter_by(api_key=api_key).first()
    if not user:
        abort(401, description="Invalid API key.")
    return user

# ────────────────────────────────────────────────────────────────────
#  Web UI routes  (unchanged)
# ────────────────────────────────────────────────────────────────────
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("preferences"))
    form = RegisterForm()
    if form.validate_on_submit():
        if User.query.filter_by(email=form.email.data.lower()).first():
            flash("Account exists – please log in.", "warning")
            return redirect(url_for("login"))
        user = User(
            email=form.email.data.lower(),
            api_key=secrets.token_hex(32),
        )
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash("Account created – welcome!", "success")
        return redirect(url_for("preferences"))
    return render_template("register.html", title="Register", form=form)

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("preferences"))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower()).first()
        if user:
            login_user(user)
            flash("Logged in.", "success")
            nxt = request.args.get("next")
            return redirect(nxt or url_for("preferences"))
        flash("Unknown account – register first.", "danger")
    return render_template("login.html", title="Login", form=form)

@app.route("/logout")
@login_required
def logout():
    if current_user.monitoring_active:
        current_user.monitoring_active = False
        db.session.commit()
    logout_user()
    flash("Logged out.", "info")
    return redirect(url_for("login"))

@app.route("/")
@app.route("/preferences")
@login_required
def preferences():
    user_cfgs = current_user.configs.order_by(ShiftConfig.created.desc()).all()
    form = PrefForm()
    all_shifts = current_user.scraped_shifts.order_by(
        ScrapedShift.scraped_at.desc()).all()
    new_shifts = current_user.scraped_shifts.filter_by(
        is_new_for_user=True).order_by(
        ScrapedShift.scraped_at.desc()).limit(50).all()
    return render_template(
        "preferences.html",
        title="Your Preferences & Setup",
        form=form,
        user_configs=user_cfgs,
        current_user_email=current_user.email,
        duo_done_status=current_user.duo_done,
        monitoring_status=current_user.monitoring_active,
        QUICK_CHECK_INTERVAL_SECONDS=TASK_QUICK_CHECK_INTERVAL,
        scraped_shifts=all_shifts,
        newly_scraped_shifts=new_shifts,
        dt=dt,
        api_key=current_user.api_key,
    )

# In app.py, add this code block after your @app.route("/preferences") function.

@app.route("/save_preference", methods=["POST"])
@login_required
def save_preference():
    """Saves a new preference config from the web UI."""
    form = PrefForm() # We bind the form to the request data
    if form.validate_on_submit():
        # TomSelect sends data as a comma-separated string if multiple items are selected.
        # This is perfect for our database model.
        new_config = ShiftConfig(
            owner=current_user,
            target_date=form.target_date.data or None,
            target_time=','.join(form.target_time.data) or None,
            target_position=','.join(form.target_position.data) or None
        )
        db.session.add(new_config)
        db.session.commit()
        flash("Preference saved successfully!", "success")
    else:
        # Handle form errors
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"Error in {getattr(form, field).label.text}: {error}", "danger")
    return redirect(url_for("preferences"))

@app.route("/clear_all_preferences", methods=["POST"])
@login_required
def clear_all_preferences():
    """Deletes all of a user's preferences."""
    num_deleted = current_user.configs.delete()
    db.session.commit()
    flash(f"{num_deleted} preference(s) cleared. Now watching for ALL new shifts.", "info")
    return redirect(url_for("preferences"))

@app.route("/launch_browser_setup")
@login_required
def launch_browser_setup():
    """Triggers the interactive Duo login task."""
    if CELERY_AVAILABLE:
        from tasks import open_login_browser_for_duo_setup
        open_login_browser_for_duo_setup.delay(current_user.id)
        flash("Duo/ScheduleSource login task initiated. A browser window should open on the server machine shortly.", "info")
    else:
        flash("Monitoring service (Celery) is not available.", "danger")
    return redirect(url_for("preferences"))

@app.route("/fetch_my_shifts")
@login_required
def fetch_my_shifts():
    """Triggers a full manual scrape for the user."""
    if CELERY_AVAILABLE:
        from tasks import trigger_shift_scrape_for_user
        trigger_shift_scrape_for_user.delay(current_user.id)
        flash("Full shift scrape has been queued. Results will appear in 'All Known Shifts' shortly (refresh to see updates).", "info")
    else:
        flash("Monitoring service (Celery) is not available.", "danger")
    return redirect(url_for("preferences"))

@app.route("/start_monitoring")
@login_required
def start_shift_monitoring():
    """Starts the continuous monitoring task."""
    current_user.monitoring_active = True
    db.session.commit()
    if CELERY_AVAILABLE and quick_check_shifts_for_user:
        # Queue the first check immediately
        quick_check_shifts_for_user.delay(current_user.id)
        flash("Continuous shift monitoring has been started!", "success")
    else:
        flash("Could not start monitoring: Service is unavailable.", "danger")
    return redirect(url_for("preferences"))

@app.route("/stop_monitoring")
@login_required
def stop_shift_monitoring():
    """Stops the continuous monitoring task."""
    current_user.monitoring_active = False
    db.session.commit()
    flash("Continuous shift monitoring has been stopped.", "info")
    return redirect(url_for("preferences"))

# (Your # MOBILE-APP JSON API ROUTES section should come after this)

# --- (other existing web routes for save_preference, launch_browser_setup,
#     fetch_my_shifts, start/stop monitoring remain unchanged – omitted
#     here for brevity, keep yours exactly as they were) -----------------
# Ensure you have these routes from your original app.py:
# @app.route("/save_preference", methods=["POST"])
# @app.route("/save_api_key", methods=["POST"]) # If this was present
# @app.route("/launch_browser_setup")
# @app.route("/fetch_my_shifts")
# @app.route("/start_monitoring")
# @app.route("/stop_monitoring")

# ────────────────────────────────────────────────────────────────────
#  MOBILE-APP JSON API ROUTES
# ────────────────────────────────────────────────────────────────────
@app.route("/api/v1/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").lower().strip()
    pwd   = (data.get("password") or "").strip()
    if not email or not pwd:
        return jsonify(status="error",
                       message="Email & password required."), 400
    if User.query.filter_by(email=email).first():
        return jsonify(status="error",
                       message="Account already exists."), 409
    u = User(email=email, api_key=secrets.token_hex(32))
    u.set_password(pwd)
    db.session.add(u); db.session.commit()
    return jsonify(status="success", api_key=u.api_key), 201

@app.route("/api/v1/login_for_key", methods=["POST"])
def api_login_for_key():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").lower().strip()
    pwd   = (data.get("password") or "").strip()
    user = User.query.filter_by(email=email).first()
    if user and user.check_password(pwd):
        return jsonify(status="success", api_key=user.api_key)
    return jsonify(status="error", message="Invalid credentials."), 401

@app.route("/api/v1/dashboard", methods=["GET"])
def api_dashboard():
    user = _auth_from_header()
    def _pref(p):  # helper
        return {
            "id": p.id,
            "target_date": p.target_date,
            "target_time": p.target_time,
            "target_position": p.target_position,
            "created": p.created.isoformat() if p.created else None,
        }
    def _shift(s):
        return {
            "shift_date_str": s.shift_date_str,
            "shift_time_str": s.shift_time_str,
            "shift_position_str": s.shift_position_str,
            "raw_text": s.raw_text,
        }
    return jsonify(
        status="success",
        monitoring_active=user.monitoring_active,
        duo_done=user.duo_done,
        preferences=[_pref(p) for p in
                     user.configs.order_by(ShiftConfig.created.desc()).all()],
        new_shifts=[_shift(s) for s in
                    user.scraped_shifts.filter_by(is_new_for_user=True).all()],
        all_shifts=[_shift(s) for s in
                    user.scraped_shifts.order_by(
                        ScrapedShift.scraped_at.desc()).all()],
    )

@app.route("/api/v1/monitoring", methods=["POST"])
def api_monitoring():
    user = _auth_from_header()
    data = request.get_json(silent=True) or {}
    enable = bool(data.get("monitoring_active"))

    app.logger.info(f"API: Setting monitoring for user {user.email} to {enable}")
    user.monitoring_active = enable
    db.session.commit()

    if enable and CELERY_AVAILABLE and quick_check_shifts_for_user:
        # Only start the task if enabling monitoring AND Celery is available
        # Check if a monitoring task is already scheduled or running for this user
        # to avoid duplicates. This is a bit complex with Celery's default inspection.
        # For simplicity now, we'll just queue it.
        # A more robust solution would involve a flag in Redis or DB to see if a task is "active".
        app.logger.info(f"API: Queuing quick_check_shifts_for_user for user {user.id} ({user.email})")
        try:
            quick_check_shifts_for_user.delay(user.id) # or .apply_async(args=[user.id])
            app.logger.info(f"API: Task successfully queued for user {user.id}.")
        except Exception as e:
            app.logger.error(f"API: Failed to queue Celery task for user {user.id}: {e}")
            # Optionally, revert monitoring_active or notify user of failure
            # user.monitoring_active = False
            # db.session.commit()
            # return jsonify(status="error", message="Failed to start monitoring task.", monitoring_active=False), 500
    elif not enable and CELERY_AVAILABLE:
        # If disabling monitoring, the task itself (quick_check_shifts_for_user)
        # will see user.monitoring_active == False and will not reschedule itself.
        # Active revocation of tasks is more complex.
        app.logger.info(f"API: Monitoring disabled for user {user.id}. Celery task will stop rescheduling on its own.")
    elif enable and not CELERY_AVAILABLE:
        app.logger.error(f"API: Cannot start monitoring for user {user.id} - Celery tasks are not available.")
        # Revert the DB change if task cannot be started
        user.monitoring_active = False
        db.session.commit()
        return jsonify(status="error", message="Monitoring service unavailable.", monitoring_active=False), 503


    return jsonify(status="success", monitoring_active=user.monitoring_active)

@app.route("/api/v1/prefs", methods=["GET", "POST"])
def api_prefs():
    user = _auth_from_header()
    if request.method == "GET":
        # This is correct
        return jsonify([p.as_dict() for p in user.configs.order_by(ShiftConfig.created.desc()).all()])
    if request.method == "POST":
        # This is correct
        data = request.get_json(silent=True) or {}
        p = ShiftConfig(
            owner=user,
            target_date=data.get("target_date"),
            target_time=data.get("target_time"),
            target_position=data.get("target_position"),
        )
        db.session.add(p); db.session.commit()
        return jsonify(p.as_dict()), 201

# This route is now for deleting a SINGLE preference by its ID
@app.route("/api/v1/prefs/<int:pref_id>", methods=["PUT", "DELETE"])
# @login_required  <-- DELETE OR COMMENT OUT THIS LINE. THIS IS THE ONLY CHANGE.
def api_manage_single_pref(pref_id):
    user = _auth_from_header()
    # Use first_or_404 to simplify error handling
    pref = ShiftConfig.query.filter_by(id=pref_id, user_id=user.id).first_or_404()

    if request.method == 'PUT':
        data = request.get_json(silent=True) or {}
        pref.target_date = data.get("target_date")
        pref.target_time = data.get("target_time")
        pref.target_position = data.get("target_position")
        db.session.commit()
        return jsonify(pref.as_dict())

    if request.method == 'DELETE':
        db.session.delete(pref)
        db.session.commit()
        return jsonify(status="success", deleted_id=pref_id)

# I also added .as_dict() to your GET and POST routes for consistency. 
# Make sure this method exists in your models.py ShiftConfig class.
# If not, here it is:

# In models.py, inside ShiftConfig class:
# def as_dict(self):
#     return {
#         "id": self.id,
#         "target_date": self.target_date,
#         "target_time": self.target_time,
#         "target_position": self.target_position,
#         "created": self.created.isoformat() if self.created else None,
#     }

@app.route("/api/v1/shifts")
def api_shifts():
    user = _auth_from_header()
    mode = request.args.get("mode", "all")  # all | new
    q = user.scraped_shifts.order_by(ScrapedShift.scraped_at.desc())
    if mode == "new":
        q = q.filter_by(is_new_for_user=True)
    return jsonify([
        {
            "shift_date_str": s.shift_date_str,
            "shift_time_str": s.shift_time_str,
            "shift_position_str": s.shift_position_str,
            "raw_text": s.raw_text,
        } for s in q.limit(300)
    ])

@app.route("/api/v1/session/update", methods=["POST"])
def api_session_update():
    user = _auth_from_header()
    payload = request.get_json(silent=True) or {}
    sess = payload.get("session_data") or {}
    if not isinstance(sess.get("cookies"), list):
        return jsonify(status="error",
                       message="session_data.cookies list required"), 400
    try:
        user_dir = os.path.join(USER_DATA_ROOT_DIR, f"user_{user.id}")
        os.makedirs(user_dir, exist_ok=True)
        with open(os.path.join(user_dir, STORAGE_STATE_FILENAME), "w") as fh:
            json.dump(sess, fh, indent=2)
        user.duo_done = True
        user.monitoring_active = False # Explicitly stop monitoring after session update
        db.session.commit()
        return jsonify(status="success"), 200
    except Exception as exc:
        app.logger.exception("save session failed")
        db.session.rollback()
        return jsonify(status="error", message=str(exc)), 500

# ────────────────────────────────────────────────────────────────────
# CLI    flask init-db
# ────────────────────────────────────────────────────────────────────
@app.cli.command("init-db")
def init_db_command():
    """Drop any existing SQLite file & create fresh tables (dev only)."""
    db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if db_uri.startswith("sqlite:///"):
        path = os.path.normpath(db_uri.replace("sqlite:///", ""))
        if os.path.exists(path):
            print("Deleting old DB:", path)
            os.remove(path)
    with app.app_context():
        db.create_all()
    print("✔ Database initialised.")

# ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True) # debug=True for development
# ────────────────────────────────────────────────────────────────────