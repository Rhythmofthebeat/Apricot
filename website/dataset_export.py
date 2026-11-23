from .models import EarnTask, TaskSubmission, ContractorProfile


def consented_user_ids():
    """Contractors who accepted the training-data licence. Only their responses
    may be sold or exported."""
    rows = ContractorProfile.query.filter(ContractorProfile.training_consent_at.isnot(None)) \
        .with_entities(ContractorProfile.user_id).all()
    return {r[0] for r in rows}


def shape_submission(task, sub):
    """Return the training-data-shaped dict for one submission, or None to
    skip it (e.g. a 'tie' ranking response carries no preference signal)."""
    if task.task_type == 'ranking':
        if sub.response == 'A':
            chosen, rejected = task.option_a, task.option_b
        elif sub.response == 'B':
            chosen, rejected = task.option_b, task.option_a
        else:
            return None
        return {
            "prompt": task.description,
            "chosen": chosen,
            "rejected": rejected,
            "annotator_note": sub.explanation or "",
        }

    if task.task_type == 'rating':
        try:
            score = float(sub.response)
        except (TypeError, ValueError):
            return None
        return {
            "prompt": task.description,
            "response": task.content,
            "score": score,
            "annotator_note": sub.explanation or "",
        }

    if task.task_type == 'factcheck':
        return {
            "response": task.content,
            "label": sub.response,
            "annotator_note": sub.explanation or "",
        }

    if task.task_type == 'writing':
        return {
            "messages": [
                {"role": "user", "content": task.description},
                {"role": "assistant", "content": sub.response},
            ],
            "annotator_note": sub.explanation or "",
        }

    return None


def build_bulk_export(task_type=None, require_consensus=True):
    """Shaped records across every EarnTask, optionally filtered to one
    task_type. require_consensus=True limits results to tasks that reached
    their min_responses threshold, so buyers only get quality-checked data.
    Calibration (assessment) tasks and responses from contractors who have not
    accepted the training-data licence are never included."""
    consented = consented_user_ids()
    query = EarnTask.query.filter(EarnTask.is_assessment.is_(False))
    if task_type:
        query = query.filter_by(task_type=task_type)

    records = []
    for task in query.all():
        submissions = TaskSubmission.query.filter_by(task_id=task.id).all()
        # Consensus is judged on everyone who answered; only consenting answers ship.
        if require_consensus and len(submissions) < task.min_responses:
            continue
        for sub in submissions:
            if sub.user_id not in consented:
                continue
            record = shape_submission(task, sub)
            if record:
                records.append(record)
    return records
