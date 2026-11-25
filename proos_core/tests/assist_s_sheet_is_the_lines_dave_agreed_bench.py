"""Assist's instruction sheet is exactly the lines Dave agreed — REGISTER 1469 (one-list 10.34).  DRIVEN.
Run: python3 tests/assist_s_sheet_is_the_lines_dave_agreed_bench.py

THE LOCK. Dave, 25 Nov 2026: "I always planned on putting some control back in assist but it got out of control so much before
as you were writing rule after rule and it was impossible to know what the platform could do." On 9 Nov he had the sheet cut to
the house's name, the white label and his own method (1267, 1278: "We are not adding lines"). He has now agreed ONE more line —
how a technician reports — on the condition that a line can never again be added without his yes.

So the sheet a platform holder gets is built here from THE LIST BELOW and compared, character for character, with what Assist
really sends (assist._system_doctrine). Any line added, dropped or reworded in assist.py closes the gate. A new line is made by
adding it to this list WITH the register entry and Dave's words — which is the record of his yes; without that, it is not his.

 1. the sheet Assist sends is exactly these lines, in this order, and nothing else
 2. it is the same sheet for a Developer and a homeowner (1273)
 3. the reporting line is on it, word for word as he agreed it
 4. the long sheet for everyone else is untouched by this
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
os.environ.setdefault("PROOS_DATA_DIR", "/tmp/proos_bench_sheet")
os.makedirs(os.environ["PROOS_DATA_DIR"], exist_ok=True)
from proos import assist as A  # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


# THE LINES, EACH WITH THE DAY DAVE AGREED IT. Nothing goes on this list without his yes.
LINES = [
    # 1267 / 1267d — 9 Nov 2026, "100% back to platform": who it is, whose tools, and the white label
    ("1267", "You are Pro Assist for '%s'. You run this house with the platform's own tools. "
             "Never say an entity id, the name of the platform underneath, or of the AI supplier — this is ProOS."),
    # 1278 — 9 Nov 2026, "Fine just put it back": his own method line of 16 Aug (222), word for word
    ("1278", "Build it the way you would by hand — READ, WRITE, RUN, READ BACK, FIX, RUN AGAIN — and a saved thing you have "
             "not run is NOT tested."),
    # 1469 — 24 Nov 2026 20:50 "Yes … I always planned on putting some control back in assist"; 20:43 "any technician that was
    # asked to look into a problem you just want to hear what the outcome is and what it's suggests to do not the whole story"
    ("1469", "Report like a technician: the outcome first, then what you suggest, in a few sentences. "
             "Keep the steps you took and the detail until asked."),
]
WANT = "".join(l + "\n" for _r, l in LINES) % ("Home",)

DEV = {"id": "dev", "is_owner": True, "is_admin": True, "platform_tools": True}
HOME = {"id": "home", "is_owner": False, "is_admin": False, "platform_tools": True}
sheet = A._platform_first_doctrine(DEV, "Home")
check("1 · the sheet Assist sends is exactly the lines Dave agreed, in order, and nothing else", sheet, WANT)
check("1 · …three lines — a fourth has not been agreed", sheet.count("\n"), len(LINES))
check("2 · a homeowner's sheet is the same sheet", A._platform_first_doctrine(HOME, "Home"), WANT)
check("3 · the reporting line is on it, word for word",
      "Report like a technician: the outcome first, then what you suggest, in a few sentences. "
      "Keep the steps you took and the detail until asked.\n" in sheet)
check("3 · …and it comes after his method, not before it", 0 <= sheet.find("READ, WRITE, RUN") < sheet.find("Report like a technician"))
full = A._system_doctrine_full({"id": "i", "is_owner": False, "is_admin": True}, "Home")
check("4 · the long sheet for everyone else does not carry it (untouched by this)", "Report like a technician" in full, False)

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)) + ("  " + repr(FAIL) if FAIL else ""))
sys.exit(1 if FAIL else 0)
