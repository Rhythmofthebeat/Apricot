from .models import EarnTask, TaskSubmission


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
    their min_responses threshold, so buyers only get quality-checked data."""
    query = EarnTask.query
    if task_type:
        query = query.filter_by(task_type=task_type)

    records = []
    for task in query.all():
        submissions = TaskSubmission.query.filter_by(task_id=task.id).all()
        if require_consensus and len(submissions) < task.min_responses:
            continue
        for sub in submissions:
            record = shape_submission(task, sub)
            if record:
                records.append(record)
    return records
