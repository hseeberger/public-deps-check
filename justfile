set shell := ["bash", "-uc"]

nightly := `rustc --version | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | sed 's/^/nightly-/'`

test:
    PUBLIC_DEPS_TOOLCHAIN={{ nightly }} python3 -m unittest discover -s tests -v

all: test
