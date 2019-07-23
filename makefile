
.PHONY: pypi help test

help:
	@echo "This is a makefile providing various convenient functions:"
	@echo " "
	@echo "help:  Prints this message."
	@echo "test:  Runs automated tests."
	@echo "pypi:  Push package to pypi (requiers appropriate permissions)."
	@echo " "

pypi: README.rst
	 python3 setup.py sdist upload -r pypi

README.rst: README.org
	pandoc --from=org --to=rst --output=README.rst README.org

test:
	py.test -v --doctest-modules tests ox_ui
