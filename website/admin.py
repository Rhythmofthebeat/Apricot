import json
import io
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, Response
from flask_login import login_required, current_user
from . import db
from .models import EarnTask, TaskSubmission, User

admin_bp = Blueprint('admin_bp', __name__)


def _require_admin():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


# ------------------------------------------------ #
# -------------------- DASHBOARD ----------------- #
# ------------------------------------------------ #

@admin_bp.route('/admin')
@login_required
def dashboard():
    _require_admin()
    tasks = EarnTask.query.order_by(EarnTask.created_at.desc()).all()
    total_submissions = TaskSubmission.query.count()
    total_paid = db.session.query(db.func.sum(TaskSubmission.earnings_cents))\
        .filter_by(paid_out=False).scalar() or 0
    return render_template('admin_dashboard.html',
        tasks=tasks,
        total_submissions=total_submissions,
        total_pending_payout=total_paid,
    )


# ------------------------------------------------ #
# ------------------- CREATE TASK ---------------- #
# ------------------------------------------------ #

@admin_bp.route('/admin/tasks/new', methods=['GET', 'POST'])
@login_required
def create_task():
    _require_admin()
    if request.method == 'POST':
        task_type = request.form.get('task_type', 'ranking')
        task = EarnTask(
            title=request.form.get('title', '').strip(),
            description=request.form.get('description', '').strip(),
            task_type=task_type,
            domain=request.form.get('domain', '').strip() or None,
            pay_cents=int(float(request.form.get('pay_dollars', '5')) * 100),
            estimated_minutes=int(request.form.get('estimated_minutes', 5)),
            option_a=request.form.get('option_a', '').strip() or None,
            option_b=request.form.get('option_b', '').strip() or None,
            content=request.form.get('content', '').strip() or None,
            status='open',
        )
        db.session.add(task)
        db.session.commit()
        flash("Task created.", "success")
        return redirect(url_for('admin_bp.dashboard'))
    return render_template('admin_task_form.html')


# ------------------------------------------------ #
# ------------------- CLOSE TASK ----------------- #
# ------------------------------------------------ #

@admin_bp.route('/admin/tasks/<int:task_id>/close', methods=['POST'])
@login_required
def close_task(task_id):
    _require_admin()
    task = EarnTask.query.get_or_404(task_id)
    task.status = 'closed'
    db.session.commit()
    flash("Task closed.", "success")
    return redirect(url_for('admin_bp.dashboard'))


# ------------------------------------------------ #
# --------- EXPORT TRAINING DATA (JSONL) --------- #
# ------------------------------------------------ #

@admin_bp.route('/admin/tasks/<int:task_id>/export')
@login_required
def export_task(task_id):
    _require_admin()
    task = EarnTask.query.get_or_404(task_id)
    submissions = TaskSubmission.query.filter_by(task_id=task_id).all()

    lines = []
    for sub in submissions:
        if task.task_type == 'ranking':
            if sub.response == 'A':
                chosen, rejected = task.option_a, task.option_b
            elif sub.response == 'B':
                chosen, rejected = task.option_b, task.option_a
            else:
                continue  # skip "tie" responses for preference data
            lines.append(json.dumps({
                "prompt": task.description,
                "chosen": chosen,
                "rejected": rejected,
                "annotator_note": sub.explanation or "",
            }))

        elif task.task_type == 'rating':
            try:
                score = float(sub.response)
            except (TypeError, ValueError):
                continue
            lines.append(json.dumps({
                "prompt": task.description,
                "response": task.content,
                "score": score,
                "annotator_note": sub.explanation or "",
            }))

        elif task.task_type == 'factcheck':
            lines.append(json.dumps({
                "response": task.content,
                "label": sub.response,
                "annotator_note": sub.explanation or "",
            }))

        elif task.task_type == 'writing':
            lines.append(json.dumps({
                "messages": [
                    {"role": "user", "content": task.description},
                    {"role": "assistant", "content": sub.response},
                ],
                "annotator_note": sub.explanation or "",
            }))

    output = "\n".join(lines)
    filename = f"task_{task_id}_{task.task_type}.jsonl"
    return Response(
        output,
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ------------------------------------------------ #
# ------------- VIEW TASK SUBMISSIONS ------------ #
# ------------------------------------------------ #

@admin_bp.route('/admin/tasks/<int:task_id>')
@login_required
def task_submissions(task_id):
    _require_admin()
    task = EarnTask.query.get_or_404(task_id)
    submissions = TaskSubmission.query.filter_by(task_id=task_id)\
        .order_by(TaskSubmission.created_at.desc()).all()
    return render_template('admin_task_submissions.html', task=task, submissions=submissions)
