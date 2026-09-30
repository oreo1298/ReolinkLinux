# ReolinkLinux — convenience targets.
# Arch-based distributions: use the PKGBUILD instead (makepkg -si).

PREFIX ?= $(HOME)/.local
PYTHON ?= python3

.PHONY: help run demo install uninstall test lint gui-check dist clean

help:
	@echo "ReolinkLinux make targets:"
	@echo "  make run          Start the app from this folder"
	@echo "  make demo         Start it with simulated demo cameras"
	@echo "  make install      Install for your user (PREFIX=$(PREFIX))"
	@echo "  make uninstall    Remove that installation"
	@echo "  make test         Run the test suite"
	@echo "  make lint         Run ruff"
	@echo "  make gui-check    Headless start-up check"
	@echo "  make dist         Build a wheel and sdist"

run:
	./reolinklinux.sh

demo:
	./reolinklinux.sh --demo

install:
	PREFIX="$(PREFIX)" ./packaging/install.sh

uninstall:
	PREFIX="$(PREFIX)" ./packaging/uninstall.sh

test:
	QT_QPA_PLATFORM=offscreen $(PYTHON) -m pytest -q

lint:
	ruff check reolinklinux tests

gui-check:
	QT_QPA_PLATFORM=offscreen $(PYTHON) -c "from PySide6.QtWidgets import QApplication; app = QApplication([]); \
	from reolinklinux.core.config import Config; from reolinklinux.gui.main_window import MainWindow; \
	import tempfile, pathlib; w = MainWindow(Config(pathlib.Path(tempfile.mkdtemp()) / 'c.json')); print('GUI OK')"

dist:
	$(PYTHON) -m build

clean:
	rm -rf build dist ./*.egg-info .pytest_cache .ruff_cache src pkg *.pkg.tar.*
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
