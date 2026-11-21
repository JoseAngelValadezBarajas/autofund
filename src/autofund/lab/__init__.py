"""AutoFund Lab: paper-only experiments with data capture you start and stop yourself.

Everything here runs inside the application process. There is no daemon, no scheduled task and no
service: when the application exits, capture and any session stop with it. Everything the lab
writes goes to the private, git-ignored ``research/`` directory.
"""
