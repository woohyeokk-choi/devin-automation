# Config for the separate Superset MCP service in the local light stack.
#
# Inherits the light stack's configuration (metastore caches, SimpleCache,
# Celery disabled) and only adds the development authentication identity the
# MCP service needs. Kept in the automation repo so the Superset checkout stays
# byte-identical to the revision under test.
from superset_config_docker_light import *  # noqa: F401,F403

# Development-mode MCP authentication (auth priority 3). Local stack only.
MCP_DEV_USERNAME = "admin"
