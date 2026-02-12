# Makefile for Control Center CLI Tool
# Simple installation and management for the command-line tool

.PHONY: help install uninstall update build test clean dev lint format doctor

# Colors for output
GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m # No Color

# Help target
help:
	@echo "$(GREEN)Control Center CLI Tool - Makefile$(NC)"
	@echo ""
	@echo "Available targets:"
	@echo "  $(YELLOW)install$(NC)     - Install Control Center CLI tool"
	@echo "  $(YELLOW)uninstall$(NC)   - Uninstall Control Center CLI tool"
	@echo "  $(YELLOW)update$(NC)      - Update to latest version from git"
	@echo "  $(YELLOW)build$(NC)       - Build Python package"
	@echo "  $(YELLOW)dev$(NC)         - Install in development mode with dev dependencies"
	@echo "  $(YELLOW)test$(NC)        - Run test suite"
	@echo "  $(YELLOW)lint$(NC)        - Run linters (pylint, mypy)"
	@echo "  $(YELLOW)format$(NC)      - Format code (black, isort)"
	@echo "  $(YELLOW)clean$(NC)       - Clean build artifacts"
	@echo "  $(YELLOW)doctor$(NC)      - Run diagnostics"
	@echo ""
	@echo "Examples:"
	@echo "  make install    # Install the tool"
	@echo "  make update     # Update to latest version"
	@echo "  make dev        # Setup development environment"

# Installation target
install:
	@echo "$(GREEN)Installing Control Center CLI tool...$(NC)"
	@pip install -e .
	@echo "$(GREEN)✓ Installation complete!$(NC)"
	@echo ""
	@echo "Setup your configuration:"
	@echo "  1. Create config:  control-center config init"
	@echo "  2. Set token:      control-center config set-token YOUR_TOKEN"
	@echo "  3. Set server:     control-center config set-server HOST PORT"
	@echo ""
	@echo "Or use environment variables:"
	@echo "  export CONTROL_CENTER_TOKEN=your-token-here"
	@echo ""
	@echo "Then connect:"
	@echo "  control-center connect --host YOUR_SERVER_IP"

# Uninstall target
uninstall:
	@echo "$(YELLOW)Uninstalling Control Center...$(NC)"
	@pip uninstall control-center -y 2>/dev/null || true
	@rm -rf ~/.config/control-center
	@rm -rf ~/Library/Application\ Support/control-center 2>/dev/null || true
	@echo "$(GREEN)✓ Uninstall complete$(NC)"

# Update target
update:
	@echo "$(GREEN)Updating Control Center...$(NC)"
	@echo "1. Pulling latest changes from git..."
	@git pull origin main
	@echo "2. Reinstalling package..."
	@pip install -e . --upgrade
	@echo "$(GREEN)✓ Update complete!$(NC)"

# Build target
build:
	@echo "$(GREEN)Building Python package...$(NC)"
	@python -m build
	@echo "$(GREEN)✓ Build complete$(NC)"

# Development installation
dev:
	@echo "$(GREEN)Installing development dependencies...$(NC)"
	@pip install -e ".[dev]"
	@echo "$(GREEN)✓ Development environment ready$(NC)"

# Test target
test:
	@echo "$(GREEN)Running tests...$(NC)"
	@pytest tests/ -v --cov=controller --cov-report=term --cov-report=html
	@echo "$(GREEN)✓ Tests complete$(NC)"
	@echo "Coverage report: htmlcov/index.html"

# Lint target
lint:
	@echo "$(GREEN)Running linters...$(NC)"
	@echo "1. Running pylint..."
	@pylint controller/ || true
	@echo "2. Running mypy..."
	@mypy controller/ || true
	@echo "$(GREEN)✓ Linting complete$(NC)"

# Format target
format:
	@echo "$(GREEN)Formatting code...$(NC)"
	@black controller/
	@isort controller/
	@echo "$(GREEN)✓ Formatting complete$(NC)"

# Clean target
clean:
	@echo "$(YELLOW)Cleaning build artifacts...$(NC)"
	@rm -rf build/ dist/ *.egg-info
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name ".coverage" -delete
	@echo "$(GREEN)✓ Clean complete$(NC)"

# Doctor target (diagnostics)
doctor:
	@echo "$(GREEN)Running diagnostics...$(NC)"
	@control-center doctor || echo "$(RED)Install the tool first: make install$(NC)"

# Quick check
check: lint test
	@echo "$(GREEN)✓ All checks passed$(NC)"

# Default target
.DEFAULT_GOAL := help