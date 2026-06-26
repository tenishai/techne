# syntax=docker/dockerfile:1
# techne — agentic memory MCP server
#
# MCP is a stdio protocol; this container is a `docker run -i` child of the
# MCP client (e.g. Claude Code).  No ports, no network healthcheck needed.
#
# Basic usage (MCP server, with persistent store):
#   docker run -i \
#     -v ~/.techne:/root/.techne \
#     techne
#
# With transcript ingest (mount the host Claude project logs read-only):
#   docker run -i \
#     -v ~/.techne:/root/.techne \
#     -v ~/.claude/projects:/root/.claude/projects:ro \
#     techne
#
# Override the store location via TECHNE_HOME if you prefer a different path:
#   docker run -i \
#     -e TECHNE_HOME=/data/techne \
#     -v /some/host/path:/data/techne \
#     techne

FROM python:3.12-slim

WORKDIR /app

# Copy repo and install as editable so running code == repo HEAD
COPY . .
RUN pip install --no-cache-dir -e ".[vectors]"

# TECHNE_HOME defaults to ~/.techne = /root/.techne inside the container.
# Declare it as a volume so `docker run` without an explicit -v still gets
# a named volume (survives container replacement) rather than an ephemeral
# anonymous layer.
ENV TECHNE_HOME=""
VOLUME ["/root/.techne"]

# stdio MCP server — receives JSON-RPC on stdin, responds on stdout
ENTRYPOINT ["python", "-m", "techne.mcp_server"]
