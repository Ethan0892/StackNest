#!/usr/bin/env python3
"""
stress_test.py — Run 3 complex plugin generation requests through the full
validation + feedback loop and print a detailed diagnostic report.

Usage:
    python stress_test.py
"""

import sys
import time
import textwrap

# Ensure project root is on sys.path
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from validation.feedback_loop import PluginGenerator, GenerationResult

# ---------------------------------------------------------------------------
# Test prompts — complex, feature-rich, known to exercise edge cases
# ---------------------------------------------------------------------------

PROMPTS = [
    (
        "BountyHunter",
        (
            "Create a full-featured BountyHunter plugin. Players can place a gold bounty on any "
            "online player using /bounty set <player> <amount>. Gold is taken from their balance "
            "(use a simple internal economy stored in a config.yml). When the target is killed by "
            "another player, the killer claims the bounty automatically and receives the gold. "
            "Commands: /bounty set <player> <amount>, /bounty list (shows top 10 active bounties "
            "in a chat GUI with player heads as items), /bounty cancel <player> (refunds if the "
            "requester cancels within 30 seconds). Track every bounty with a persistent "
            "PersistentDataContainer key on the player. Broadcast a server-wide Adventure Component "
            "message when a bounty is placed or claimed. Include full plugin.yml and a test class."
        ),
    ),
    (
        "MagicEnchants",
        (
            "Create a MagicEnchants plugin that adds 3 custom enchantments to swords using the "
            "PersistentDataContainer (no NMS). Enchantment 1 — Lifesteal: heals the attacker for "
            "15% of damage dealt. Enchantment 2 — Shockwave: on hit, launches nearby entities "
            "within 4 blocks into the air with a velocity of 0.8. Enchantment 3 — Freeze: on hit "
            "with a 20% chance, applies SlowDigging + Slow potion effects for 3 seconds. "
            "Players apply enchantments via /enchant <lifesteal|shockwave|freeze>. "
            "Enchant level is stored as an integer in PersistentDataContainer on the ItemStack. "
            "All effects trigger from EntityDamageByEntityEvent. Include cooldown tracking per "
            "player in a HashMap. Use Adventure API for all messages, full plugin.yml, and a "
            "JUnit test class covering each enchantment trigger."
        ),
    ),
    (
        "BanSystem",
        (
            "Create a BanSystem plugin with the following features: "
            "/ban <player> <duration> <reason> — temp-bans a player (duration like 1h, 30m, 7d). "
            "Uses BanList.Type.NAME for the ban with an expiry Date computed from the duration string. "
            "The reason is stored as a plain String, displayed via Component.text(). "
            "/unban <player> — removes the ban. "
            "/banlist — paginates all active bans in chat, 5 per page, with /banlist <page>. "
            "/checkban <player> — shows remaining time and reason. "
            "Persist bans independently in a bans.yml file (in addition to Bukkit's ban list) so "
            "data survives server restarts. On login attempt by a banned player, kick them with a "
            "formatted Adventure Component message showing the reason and remaining time. "
            "Parse duration strings like '30m', '2h', '7d' into milliseconds. "
            "Include full plugin.yml with all commands and permissions, and a JUnit test class."
        ),
    ),
]


def run_test(name: str, prompt: str, index: int) -> GenerationResult:
    print(f"\n{'='*70}")
    print(f"TEST {index}/3 — {name}")
    print(f"{'='*70}")
    print(textwrap.fill(prompt[:200] + ("..." if len(prompt) > 200 else ""), 70))
    print()

    gen = PluginGenerator(tier="pro", plan="pro")
    t0 = time.time()
    result = gen.generate(prompt)
    elapsed = time.time() - t0

    status = "✓ PASS" if result.success else "✗ FAIL"
    print(f"\n{status}  |  attempts={result.attempts}  |  elapsed={elapsed:.1f}s")

    if result.compile_result:
        c = result.compile_result
        print(f"Compile : {'OK' if c.success else 'FAIL'}")
        if not c.success and c.errors:
            for e in c.errors[:5]:
                print(f"  compile> {e}")

    if result.yml_result:
        y = result.yml_result
        print(f"YML     : {'OK' if y.valid else 'FAIL'}")
        if not y.valid:
            for e in y.errors[:3]:
                print(f"  yml>    {e}")

    if result.static_warnings:
        print(f"Warnings: {len(result.static_warnings)}")
        for w in result.static_warnings[:3]:
            print(f"  warn>  {w}")

    if result.final_errors:
        print(f"Final errors ({len(result.final_errors)}):")
        for e in result.final_errors[:5]:
            print(f"  err>   {e}")

    if result.test_compile_result:
        tc = result.test_compile_result
        if tc.files_compiled > 0:
            print(f"TestComp: {'OK' if tc.success else 'FAIL'}  ({tc.files_compiled} file(s))")
            if not tc.success and tc.errors:
                for e in tc.errors[:3]:
                    print(f"  tcomp> {e}")

    return result


def main() -> None:
    print("StackNest Plugin Generator — Stress Test")
    print(f"Running {len(PROMPTS)} complex prompts through full validation loop\n")

    results = []
    for i, (name, prompt) in enumerate(PROMPTS, start=1):
        r = run_test(name, prompt, i)
        results.append((name, r))

    # ── Summary table ────────────────────────────────────────────────────── #
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    passes = sum(1 for _, r in results if r.success)
    print(f"Passed: {passes}/{len(results)}\n")
    for name, r in results:
        status = "PASS" if r.success else "FAIL"
        compile_ok = (r.compile_result.success if r.compile_result else False)
        yml_ok     = (r.yml_result.valid        if r.yml_result     else False)
        print(
            f"  [{status}] {name:<20} "
            f"compile={'OK' if compile_ok else 'FAIL':4}  "
            f"yml={'OK' if yml_ok else 'FAIL':4}  "
            f"attempts={r.attempts}  "
            f"elapsed={r.elapsed_seconds:.0f}s"
        )

    if passes < len(results):
        print("\nFailed tests — top errors:")
        for name, r in results:
            if not r.success and r.final_errors:
                print(f"\n  {name}:")
                for e in r.final_errors[:3]:
                    print(f"    {e}")


if __name__ == "__main__":
    main()
