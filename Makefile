.PHONY: install migrate seed run check test lint clean bootstrap

install:
	pip install -r requirements.txt

migrate:
	alembic upgrade head

seed:
	python -m medicore.seed.seeder

check:
	pytest medicore/tests/ -v

test: check

run:
	uvicorn main:app --host 0.0.0.0 --port 8000 --reload

bootstrap:
	python bootstrap.py

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
