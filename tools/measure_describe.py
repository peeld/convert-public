"""Measure "Describe a conversion" against the real AI service.

Needs ANTHROPIC_API_KEY (or a key saved in the app) and a working ffmpeg set in PeelConvert's
settings.  For each line of describe_prompts.txt it runs the agent and prints one row:
outcome, rounds, tokens, seconds.  Nothing is saved.   Run from the python folder:

    python tools/measure_describe.py [--model NAME] [--limit N]
"""
import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from convert import agent_loop, describe_settings   # noqa: E402
from convert.agent_model import AnthropicModel       # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=describe_settings.load()["model"])
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    key = os.environ.get("ANTHROPIC_API_KEY") or describe_settings.get_key()
    if not key:
        sys.exit("Set ANTHROPIC_API_KEY first.")
    prompts = [p.strip() for p in (HERE / "describe_prompts.txt").read_text().splitlines() if p.strip()]
    if args.limit:
        prompts = prompts[:args.limit]
    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    wins = 0
    print("%-8s %6s %8s %8s %6s  %s" % ("outcome", "rounds", "in", "out", "secs", "request"))
    for text in prompts:
        events = []
        session = agent_loop.Session(AnthropicModel(key, args.model), events.append)
        start = time.time()
        session.send(text)
        done = next(e for e in events if e["type"] == "done")
        outcome = ("proposed" if any(e["type"] == "proposal" for e in events)
                   else "error" if any(e["type"] == "error" for e in events)
                   else "question" if any(e["type"] == "question" for e in events) else "no-proposal")
        wins += outcome == "proposed"
        for k in total:
            total[k] += done["usage"][k]
        print("%-8s %6d %8d %8d %6.1f  %s" % (outcome, done["rounds"], done["usage"]["input"],
                                             done["usage"]["output"], time.time() - start, text))
    print("\nproposed %d of %d; tokens %s" % (wins, len(prompts), total))


if __name__ == "__main__":
    main()
