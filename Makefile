# Antonagents — common self-host commands. Run `make help` to list them.
COMPOSE     ?= docker compose
TASK_IMAGE  ?= superagent-task:latest
PROFILES    ?=            # e.g. `make up PROFILES=litellm` to also start the gateway

.PHONY: help env task-image rebuild-task up up-litellm down logs test setup

help:  ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

env:  ## Create .env from .env.example (with a generated SESSION_SECRET) if it's missing
	@if [ ! -f .env ]; then \
	  cp .env.example .env; \
	  python3 -c "import secrets;print('SUPERAGENT_SESSION_SECRET='+secrets.token_urlsafe(48))" >> .env; \
	  echo ">> Created .env — set a provider key (e.g. ANTHROPIC_API_KEY) in it before running agents."; \
	fi

task-image:  ## Build the per-run task image (agent sandbox) if it isn't built yet
	@docker image inspect $(TASK_IMAGE) >/dev/null 2>&1 \
	  || docker build -t $(TASK_IMAGE) -f docker/Dockerfile.task agent/

rebuild-task:  ## Force-rebuild the task image (after changing agent/ or Dockerfile.task)
	docker build -t $(TASK_IMAGE) -f docker/Dockerfile.task agent/

up: env task-image  ## Start the app (add PROFILES=litellm to also start the LiteLLM gateway)
	$(COMPOSE) $(if $(PROFILES),--profile $(PROFILES),) up --build

up-litellm: env task-image  ## Start the app + the optional LiteLLM gateway (OpenAI/Gemini/etc.)
	$(COMPOSE) --profile litellm up --build

down:  ## Stop the app
	$(COMPOSE) down

logs:  ## Tail the app logs
	$(COMPOSE) logs -f app

setup: env task-image  ## Prepare .env + build the task image, without starting
	@echo ">> Ready. Set your provider key in .env, then run: make up"

test:  ## Install deps + run the test suite
	pip install -r requirements.txt -r requirements-dev.txt && pytest -q
