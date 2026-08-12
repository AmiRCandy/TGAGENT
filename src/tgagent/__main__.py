"""Allow ``python -m tgagent`` as well as the ``tgagent`` console script."""

from tgagent.interfaces.cli import main

if __name__ == "__main__":
    main()
