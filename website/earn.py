from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, jsonify
from flask_login import login_required, current_user
from . import db
from .models import EarnTask, TaskSubmission

earn = Blueprint('earn', __name__)


@earn.route('/earn')
@login_required
def tasks():
    open_tasks = EarnTask.query.filter_by(status='open').order_by(EarnTask.created_at.desc()).all()
    completed_ids = {s.task_id for s in TaskSubmission.query.filter_by(user_id=current_user.id).all()}
    total_earned = db.session.query(db.func.sum(TaskSubmission.earnings_cents))\
        .filter_by(user_id=current_user.id).scalar() or 0
    pending_payout = db.session.query(db.func.sum(TaskSubmission.earnings_cents))\
        .filter_by(user_id=current_user.id, paid_out=False).scalar() or 0
    return render_template('earn_tasks.html',
        tasks=open_tasks,
        completed_ids=completed_ids,
        total_earned=total_earned,
        pending_payout=pending_payout,
    )


@earn.route('/earn/task/<int:task_id>', methods=['GET', 'POST'])
@login_required
def task_detail(task_id):
    task = EarnTask.query.get_or_404(task_id)
    if task.status != 'open':
        flash("This task is no longer available.", "error")
        return redirect(url_for('earn.tasks'))

    already_done = TaskSubmission.query.filter_by(task_id=task_id, user_id=current_user.id).first()
    if already_done:
        flash("You've already completed this task.", "success")
        return redirect(url_for('earn.tasks'))

    if request.method == 'POST':
        response = request.form.get('response', '').strip()
        explanation = request.form.get('explanation', '').strip()

        if not response:
            flash("Please select or provide a response.", "error")
            return render_template('earn_task_detail.html', task=task)

        sub = TaskSubmission(
            task_id=task_id,
            user_id=current_user.id,
            response=response,
            explanation=explanation,
            earnings_cents=task.pay_cents,
        )
        db.session.add(sub)
        db.session.commit()

        flash(f"Task complete! ${task.pay_cents / 100:.2f} added to your earnings.", "success")
        return redirect(url_for('earn.tasks'))

    return render_template('earn_task_detail.html', task=task)


@earn.route('/earn/history')
@login_required
def history():
    submissions = TaskSubmission.query.filter_by(user_id=current_user.id)\
        .order_by(TaskSubmission.created_at.desc()).all()
    total = sum(s.earnings_cents for s in submissions)
    pending = sum(s.earnings_cents for s in submissions if not s.paid_out)
    return render_template('earn_history.html', submissions=submissions, total=total, pending=pending)
