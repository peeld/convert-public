"""python -m convert  is the same as  python -m convert.check"""
import sys

from .check import main

sys.exit(main(sys.argv[1:]))
