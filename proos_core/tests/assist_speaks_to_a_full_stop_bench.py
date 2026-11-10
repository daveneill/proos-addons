"""A SPOKEN ANSWER STOPS AT A FULL STOP, AND USUALLY DOES NOT STOP AT ALL.
Run: python3 tests/assist_speaks_to_a_full_stop_bench.py

17 Sep 2026, register 483. Dave: "quite often on a long response it stops mid
way and says rest is on screen."

TWO FAULTS, AND THE PRODUCT'S OWN RULEBOOK NAMES THE FIRST ONE. Core tells
Assist, in proos/knowledge/speaking.md, that "a question gets a proper, helpful
answer at natural length" -- and the speech path then guillotined that answer at
_SPEAK_CAP = 400 characters, which is about seventy words, or three sentences.
So the product asked for a natural answer and then refused to say it. Worse, the
cut was taken at a WORD boundary, so the home stopped in the middle of a
sentence and announced that the rest was on screen.

MEASURED against the shipped 400-char rule with six real-shaped replies:
    short confirmation (47)        400: whole          now: whole
    ordinary answer (260)          400: whole          now: whole
    natural-length answer (606)    400: CUT at 422     now: WHOLE
    lead + detail (781)            400: CUT mid-sen.   now: WHOLE
    one long paragraph (904)       400: CUT mid-sen.   now: WHOLE
    2,305 chars, lead + detail     400: CUT at 422     now: the LEAD, entire
    1,830-char single paragraph    400: CUT mid-sen.   now: stops on a full stop

THE RULE, in _speak_trim(msg) -> (spoken, cut):
  1. within the ceiling PLUS SLACK, spoken whole and nothing announced -- a
     reply only just over is not worth interrupting a person for;
  2. otherwise the LEAD PARAGRAPH, if it is a real answer on its own and fits.
     Assist leads with the answer and follows with detail; detail is what a
     screen is for;
  3. otherwise the last SENTENCE END before the ceiling;
  4. and only where there is no sentence end at all, the old word boundary.

PINNED:
 1. ONE trimmer -- no copy of the cap arithmetic left anywhere in server.py
 2. both speech paths go through it: the room speaker and the glass
 3. an ordinary answer is spoken WHOLE and nothing is announced
 4. a lead paragraph is spoken ENTIRE, never clipped, when it is the answer
 5. a tiny lead is never passed off as the answer on its own
 6. a long single paragraph stops ON A SENTENCE END -- this is the defect
 7. the word-boundary fallback still exists for text with no sentence in it
 8. the announcement appears if and only if something was actually left out
 9. what is spoken is speech, never markdown (register 970uu)
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_speak_")

import server as srv                                             # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


if not hasattr(srv, "_speak_trim"):            # the OLD file runs RED, not green
    srv._speak_trim = lambda m: (str(m), False)

TAIL = getattr(srv, "_SPEAK_TAIL", " — the rest is on screen.")
CAP = srv._SPEAK_CAP
SLACK = getattr(srv, "_SPEAK_SLACK", 0)
ROOM = CAP + SLACK

# -- 1 . ONE TRIMMER, AND BOTH DOORS USE IT -------------------------------
SRC = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
check("there is one trimmer", SRC.count("def _speak_trim(") == 1)
# RE-PINNED WITH CAUSE, REGISTER 1289: both doors still go through the one trimmer; only a room mic's answer (whole=True,
# it has no screen for "the rest") is said in full — still made plain by _speak_plain, never markdown.
check("  ...the room speaker goes through it",
      "msg, _was_cut = (_speak_plain(reply), False) if whole else _speak_trim(reply)" in SRC)
check("  ...and the glass goes through it too",
      "msg, cut = (_speak_plain(msg), False) if whole else _speak_trim(msg)" in SRC)
# The arithmetic that made the cut must exist in exactly one place. If a copy
# survives anywhere, one of the two voices can drift away from the other --
# which is how the product ended up saying different things on a phone and a
# speaker before now.
check("  ...and no copy of the cut arithmetic is left behind",
      len(re.findall(r"rsplit\(\" \", 1\)\[0\]", SRC)), 1)
check("  ...the announcement is written once, not typed at each site",
      SRC.count('"\\u2014 the rest is on screen."') +
      SRC.count("— the rest is on screen.") <= 2)

# -- 2 . THE SIZE OF AN ANSWER THE HOME WILL SAY --------------------------
# speaking.md promises "a proper, helpful answer at natural length". 400
# characters is about seventy words. This is the number that made it fire
# "quite often", and it must never silently go back.
check("the ceiling is no longer three sentences", CAP >= 800)
check("  ...and there is slack, so a near miss is not announced", SLACK > 0)

# -- 3 . AN ORDINARY ANSWER IS SPOKEN WHOLE -------------------------------
SHORT = "Done - the Office is playing 774 ABC Melbourne."
got, cut = srv._speak_trim(SHORT)
check("a confirmation is said exactly as written", (got, cut), (SHORT, False))

# The answer that made Dave report this. At 606 characters it is a normal,
# helpful reply -- and the 400-char rule cut it at 422, mid-sentence.
ORDINARY = ("There are three speakers in the house not yet committed to a room: the one in the "
            "Study, the pair on the Deck, and a portable that has been moving between the "
            "Kitchen and the Bedroom all week. Until a speaker is committed it will not appear "
            "in any scene, it cannot join a group, and Assist will not speak an answer through "
            "it. Committing one takes a moment in the installer console: open the room, pick "
            "the speaker from what the home can see, and confirm. The portable is the only one "
            "worth thinking about, because it needs whichever room it spends most time in.")
got, cut = srv._speak_trim(ORDINARY)
check("a natural-length answer is said WHOLE, and nothing is announced",
      (got, cut), (ORDINARY, False))
check("  ...and this is exactly the answer the 400-char rule cut in half",
      len(ORDINARY) > 400)

# A reply a little past the ceiling: the slack exists so the home does not
# interrupt a person to announce that forty characters are on screen.
JUST_OVER = ("This is a sentence. " * 200)[:CAP + 60]
got, cut = srv._speak_trim(JUST_OVER)
check("a reply only just over the ceiling is still said whole",
      (got.strip() == JUST_OVER.strip(), cut), (True, False))
check("  ...and it really was over the ceiling", len(JUST_OVER) > CAP)

# -- 4 . THE LEAD PARAGRAPH IS THE ANSWER ---------------------------------
LEAD = ("The Family Room amplifier is offline and has been since 6:12 this morning. Everything "
        "else in that room answered normally, so this is the amplifier alone, and the most "
        "likely cause is its network switch port rather than the unit itself. I can power-cycle "
        "that port from here if you want me to.")
LONG = LEAD + "\n\n" + ("The plug reports power. It has not answered a probe in four hours. " * 30)
got, cut = srv._speak_trim(LONG)
check("a long answer says its LEAD PARAGRAPH, entire", got, LEAD + TAIL)
check("  ...and says that there is more", cut)
check("  ...the lead is not clipped anywhere inside it", LEAD in got)

# A lead that is not an answer on its own must not be passed off as one. "Sure."
# followed by six hundred words is not a spoken reply.
TINY = "Sure.\n\n" + ("A long explanation follows. " * 60)
got, cut = srv._speak_trim(TINY)
check("a one-word lead is never the whole spoken answer", len(got) > 200)
check("  ...and what is said still carries on past it",
      "A long explanation follows." in got)

# -- 5 . AND OTHERWISE, A FULL STOP. THIS IS THE DEFECT. ------------------
PARA = "The home has twenty-two committed devices across nine rooms. " * 30
got, cut = srv._speak_trim(PARA)
body = got[:-len(TAIL)] if got.endswith(TAIL) else got
check("a long single paragraph is cut", cut)
check("  ...AND IT STOPS ON A FULL STOP, never mid-sentence", body.rstrip().endswith("."))
check("  ...it stops at the ceiling, not beyond it", len(body) <= CAP)
check("  ...and it is a real answer, not a token", len(body) > CAP // 2)
check("  ...and it says the rest is on screen", got.endswith(TAIL))

MIXED = ("Who is playing where, room by room. " * 20) + ("word " * 300)
got, cut = srv._speak_trim(MIXED)
body = got[:-len(TAIL)]
check("a sentence end is preferred even when plain words follow it",
      body.rstrip().endswith("."))

# But not at any price. A single full stop near the very start is not a place
# to stop a spoken answer -- it would throw away most of what could be said.
EARLY = "Yes. " + ("word " * 400)
got, cut = srv._speak_trim(EARLY)
body = got[:-len(TAIL)]
check("  ...but a full stop in the first few words is NOT taken as the end",
      len(body) > CAP // 3)

# -- 6 . THE OLD FALLBACK STILL EXISTS ------------------------------------
# Text with no sentence in it at all -- a pasted list, a log line -- must still
# be cut somewhere sane rather than through the middle of a word.
NOSENT = "word " * 400
got, cut = srv._speak_trim(NOSENT)
body = got[:-len(TAIL)]
check("text with no sentence in it is still cut", cut)
check("  ...at a word boundary, never through a word", body.endswith("word"))
check("  ...and it still says the rest is on screen", got.endswith(TAIL))

# -- 7 . NOTHING IS ANNOUNCED THAT WAS NOT LEFT OUT -----------------------
# The half of the complaint that is about trust: if the home says the rest is
# on screen, there has to BE a rest.
for label, text in (("a confirmation", SHORT), ("an ordinary answer", ORDINARY),
                    ("a near miss", JUST_OVER)):
    got, cut = srv._speak_trim(text)
    check("nothing is announced for " + label,
          (cut, got.endswith(TAIL)), (False, False))
for label, text in (("a lead and detail", LONG), ("a long paragraph", PARA),
                    ("wordy text", NOSENT)):
    got, cut = srv._speak_trim(text)
    check("something IS announced for " + label + ", and less is said than was written",
          (cut, got.endswith(TAIL), len(got) < len(text)), (True, True, True))

# -- 8 . IT CANNOT THROW ON THE WAY TO A SPEAKER --------------------------
# This runs inside the reply path. A crash here would silence an answer that
# was otherwise fine, so the empty and odd cases are pinned as behaviour.
check("empty text is nothing, said as nothing", srv._speak_trim(""), ("", False))
check("  ...None is not a crash", srv._speak_trim(None), ("", False))
check("  ...surrounding whitespace is dropped", srv._speak_trim("  hi  "), ("hi", False))

# -- 9 . SPEECH, NEVER MARKDOWN (register 970uu) ---------------------------
# Dave, 24 Oct: the Office's room microphone answered through the room's speaker reading "asterisk asterisk" aloud.
got, _ = srv._speak_trim("Tomorrow in **Melbourne**: *sunny*, high of **24°C**.")
check("9 · bold and italic markers are not spoken; the words are", got, "Tomorrow in Melbourne: sunny, high of 24°C.")
got, _ = srv._speak_trim("## Weather\n- **Morning:** cloudy\n- **Afternoon:** clear\n1. Take a jacket")
check("  …headings, bullets and numbering come off a list", ("#" in got, "*" in got, got.startswith("Weather")),
      (False, False, True))
check("  …code marks too", srv._speak_trim("Run `restart` now")[0], "Run restart now")
check("  …and a lone star is never read as 'asterisk'", "*" in srv._speak_trim("5 * 3 is fifteen")[0], False)
check("  …the room speaker and the glass both get it (one trimmer, plain first)",
      "msg = _speak_plain(msg)" in SRC.split("def _speak_trim(")[1][:4000])

print("\n%d checks passed, %d failed%s"
      % (PASS, len(FAIL), ("  " + repr(FAIL)) if FAIL else ""))
sys.exit(1 if FAIL else 0)
