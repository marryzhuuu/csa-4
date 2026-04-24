all: format lint test

format:
	poetry run ruff format .

lint:
	poetry run ruff check --fix .

test:
	poetry run pytest -v

coverage:
	poetry run coverage run -m pytest && \
	poetry run coverage report && \
	poetry run coverage html
	
test-update-golden:
	poetry run pytest . -v --update-goldens

compile:
	poetry run python src/compiler.py $(ARGS)

run:
	poetry run python src/emulator.py $(ARGS)

