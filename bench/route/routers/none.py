"""Does nothing: the unrouted board scored as it is (harness self-test; expected completion 0 %)."""
NAME = "none (unrouted baseline)"


def available():
    return True, ""


def route(project_dir, pcb, options):
    return {"routed": 0, "total": None, "failed": [], "skipped": [], "plan": None, "phases": {}}
