"""Keep test collection isolated from production services and credentials."""

from evals.offline_env import configure_offline

configure_offline()
