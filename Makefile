.PHONY: help test build

help:
	@echo 'test   Run the standard-library test suite'
	@echo 'build  Build the standalone Python zipapp'

test:
	@python -m unittest discover -s tests -q

build:
	@python tools/build.py
