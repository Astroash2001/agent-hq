"""
Build the edge-case fixtures, and compute their ground truth independently.

"Independently" is the point. The truths below are computed with plain Python
over the same rows that were written, never by asking `query_data` -- a check
that uses the tool under test to verify the tool under test agrees with itself
and proves nothing. Seeded, so the file and its truths regenerate identically.

    python backend/data/make_edge_fixtures.py
"""
import csv
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = 30000

CLUBS = ["Real Madrid", "Barcelona", "Bayern Munich", "Liverpool", "Inter Milan",
         "Paris Saint-Germain", "AC Milan", "Ajax", "Benfica", "Manchester United"]
POSITIONS = ["GK", "DF", "MF", "FW"]


def build_players():
    """Deterministic rows, with three deliberate traps in them."""
    rng = random.Random(20260914)
    rows = []
    for i in range(ROWS):
        minutes = rng.randint(90, 1400)
        goals = rng.randint(0, 18) if i % 4 == 3 else rng.randint(0, 4)
        rows.append({
            # TRAP 1: a leading-zero identifier. It must stay text -- read as a
            # number it becomes 1234 and stops matching anything.
            "squad_no": f"{i % 99:05d}",
            # TRAP 2: multibyte names, so character counts and byte counts differ.
            "player": f"Ünïcodé Plåyer {i}",
            "club": CLUBS[i % len(CLUBS)],
            "position": POSITIONS[i % len(POSITIONS)],
            "minutes": minutes,
            "goals": goals,
            "assists": rng.randint(0, 12),
            # TRAP 3: a float that does not survive naive rounding.
            "rating": round(rng.uniform(5.5, 9.5), 2),
        })
    return rows


def truth(rows):
    """Every answer the prompt asks for, by hand."""
    total_goals = sum(r["goals"] for r in rows)
    total_minutes = sum(r["minutes"] for r in rows)

    by_club = {}
    for r in rows:
        by_club.setdefault(r["club"], {"goals": 0, "players": 0})
        by_club[r["club"]]["goals"] += r["goals"]
        by_club[r["club"]]["players"] += 1
    top_club = max(by_club.items(), key=lambda kv: (kv[1]["goals"], kv[0]))

    # Best goals-per-90 among players with a real sample, so the answer is not
    # one lucky substitute appearance.
    eligible = [r for r in rows if r["minutes"] >= 900]
    best = max(eligible, key=lambda r: (r["goals"] / (r["minutes"] / 90), r["player"]))
    best_per90 = best["goals"] / (best["minutes"] / 90)

    fw = [r for r in rows if r["position"] == "FW"]
    return {
        "total_rows": len(rows),
        "total_goals": total_goals,
        "total_minutes": total_minutes,
        "distinct_clubs": len(by_club),
        "top_club_by_goals": top_club[0],
        "top_club_goals": top_club[1]["goals"],
        "forwards": len(fw),
        "forward_avg_rating": round(sum(r["rating"] for r in fw) / len(fw), 4),
        "players_over_900_minutes": len(eligible),
        "best_goals_per90_player": best["player"],
        "best_goals_per90": round(best_per90, 4),
        "best_goals_per90_goals": best["goals"],
        "best_goals_per90_minutes": best["minutes"],
        "squad_no_is_text": rows[1]["squad_no"],
    }


def main():
    rows = build_players()

    csv_path = os.path.join(HERE, "ucl_edge_players.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    # A letter, not a table. query_data must refuse it rather than making
    # `dear_analytics` a column and answering from prose.
    with open(os.path.join(HERE, "ucl_edge_memo.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            "Dear analytics team,\n\n"
            "The attached export covers the full competition. Note that squad\n"
            "numbers are zero-padded and must be treated as labels, not numbers.\n"
            "Ratings are to two decimals.\n\n"
            "Regards,\nThe data desk\n")

    facts = truth(rows)
    with open(os.path.join(HERE, "ucl_edge_truth.json"), "w", encoding="utf-8") as fh:
        json.dump(facts, fh, indent=2)

    size = os.path.getsize(csv_path)
    with open(csv_path, encoding="utf-8") as fh:
        chars = len(fh.read())
    print(f"wrote {csv_path}")
    print(f"  {facts['total_rows']:,} rows | {size:,} bytes | {chars:,} characters")
    print(f"  bytes != characters ({size - chars:,} multibyte) -- as intended")
    print("\nground truth:")
    for k, v in facts.items():
        print(f"  {k:28} {v}")


if __name__ == "__main__":
    main()
