.PHONY: dev down build logs release deploy deploy-local

## ── Local development ────────────────────────────────────────────────────────

# Build image from source and start (rebuilds when source changes)
dev:
	docker compose up --build

# Stop local containers
down:
	docker compose down

# Build image without starting
build:
	docker build -t life-tracker:dev .

# Tail logs from running container
logs:
	docker compose logs -f

## ── Release workflow ─────────────────────────────────────────────────────────

# Create and push a version tag — GitHub Actions will build and push the image.
# Usage: make release VERSION=v1.0.0
release:
	@[ -n "$(VERSION)" ] || (echo "Error: VERSION is required  (e.g. make release VERSION=v1.0.0)" && exit 1)
	@echo "Tagging $(VERSION) and pushing to origin..."
	git tag $(VERSION)
	git push origin $(VERSION)
	@echo ""
	@echo "Done. GitHub Actions will build and push:"
	@echo "  ghcr.io/nctlcnt/life_tracker:$(VERSION)"
	@echo "  ghcr.io/nctlcnt/life_tracker:stable"

## ── Production deployment (run on the server) ────────────────────────────────

# Pull a specific version and restart the production container.
# Usage: make deploy VERSION=v1.0.0
deploy:
	@[ -n "$(VERSION)" ] || (echo "Error: VERSION is required  (e.g. make deploy VERSION=v1.0.0)" && exit 1)
	VERSION=$(VERSION) docker compose -f docker-compose.prod.yml pull
	VERSION=$(VERSION) docker compose -f docker-compose.prod.yml up -d
	@echo "Deployed $(VERSION)"

# Build the image from the working tree and restart prod — skips ghcr.io entirely.
# No version tag, no rollback archive. Use for fast iteration on the server.
deploy-local:
	docker compose -f docker-compose.prod.yml -f docker-compose.local.yml up -d --build
	@echo "Deployed local build (life-tracker:local)"
