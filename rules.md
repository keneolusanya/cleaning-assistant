# Suite Chore Bot — How It Works

**Status: proposal. Please change things.** This is a starting point, not a finished system. Tell me what's wrong with the point values, the timing, or anything else, and I'll change it before we start. It works better if it's ours rather than mine.

---

## The idea

A text-message assistant that keeps track of chores for the suite. You don't install anything or log in anywhere. It texts you, you text back normally, and it figures out the rest.

It's not a fixed rotation. It keeps a running tally of who's done what, asks whoever is furthest behind, and reshuffles when people are busy. Saying "can't this week" is a normal, allowed answer.

---

## What's in the system

| Chore | Points | How often |
|---|---|---|
| Trash | 1 | Every 3 days, or when someone reports it |
| Dishes | 0.25 for washing your own, 0.75 if you also help with other people's | A sink check to everyone every 3–4 days |
| Bathroom — we have 2, each counts separately | 3 per bathroom (split into 4 parts worth 0.75 each) | Weekends |
| Kitchen reset | 2 | Weekly |
| Fridge clear-out | 1 | Every 2 weeks, or when someone brings it up |
| Common area | 1.5 | Every week and a half, or when reported |

If nobody takes a chore, the bot asks again a few days later. Something someone reported gets asked about daily until it's done.

**Not in the system:** your own room, and cleaning up after you cook. Cooking cleanup stays on whoever cooked — we've been fine about that and the bot shouldn't nag about it.

**Points reflect effort, not importance.** A bathroom is worth more than trash because it takes longer and is worse. If a number feels wrong, say so.

---

## What "done" means

Writing this down now prevents most arguments later.

- **Bathroom** — each bathroom splits into four parts worth 0.75 each: toilet, sink + mirror, shower, floor. All four = the full 3 points for that bathroom. Doing only some is completely fine and earns partial credit — two parts gets you 1.5, and the rest goes to someone else or waits for next weekend. The bot will ask what you got to.
- **Kitchen reset** — counters, stovetop, microwave, sweep the floor. Not the same as cleaning up after yourself when you cook.
- **Fridge clear-out** — toss anything expired or spoiled that isn't labeled, and wipe the shelves.
- **Common area** — vacuum the rug, tidy up, quick sweep of the hallway.
- **Trash** — take it out and put a new bag in.
- **Dishes** — wash whatever in the sink is yours. If you have a minute, help with the rest — that's worth more.

---

## How it decides who to ask

Everyone has a running point total and a **fair share**, the amount you'd be expected to have done by now. Whoever is furthest behind their share gets asked first. If two people are tied, it asks whoever did that particular chore longest ago.

For most chores, everyone's share is equal. **The kitchen is different**, because some of us use it a lot more than others. Kitchen reset and fridge clear-out are split by how much you use the kitchen:

| How much you use the kitchen | Who (proposed — tell me if yours is wrong) | Share of kitchen work |
|---|---|---|
| A lot | Kene, Salajan, Alula | 3 parts each (~19%) |
| Sometimes | Yonas, Schneider, Jones | 2 parts each (~13%) |
| Rarely | Allahji | 1 part (~6%) |

So if you cook a lot, you'll get asked about the kitchen more, and those points count toward what you owe rather than letting you skip bathrooms. Tiers can change if habits do. Bring it up with the group, and Kene will update it.

**Declining is always free.** Saying "busy" costs you nothing. But since your total stays where it is, you'll naturally be near the front of the line next time.

**Covering earns extra.** If you take something someone else passed on, you get 1.25× the points. There should be a reason to say yes.

**Anyone can report.** Text the bot that the trash is full or send a photo of the sink, and it'll find someone to handle it. One catch to keep this honest: if you're the one furthest behind when you report something, the bot asks *you* first. You can't use reports to hand off work.

**The fridge always gets a heads-up.** When someone says the fridge needs clearing out, everyone gets a text first: label or claim anything you want to keep. Nobody's asked to clear it until a day later, so nobody's food gets tossed without warning.

**It watches for lopsided patterns,** starting after the first month (it needs some history first). If one or two people end up doing most of one particular chore, the bot starts steering that chore toward other people. In your recap, it only mentions your own share.

---

## Sink checks

Every 3–4 days (not on a fixed schedule), the bot texts all of us something like: *"Got anything in the sink? If so, wash it — and if you've got a minute, help with the rest."*

- "Nothing of mine" → totally fine, no points either way
- Washed your own → 0.25
- Helped with other people's → 0.75

No nudges on these. If you don't answer, it just lets it go. If someone washed everything in the last day, it skips the check.

---

## When it texts you

- **It never starts a text between 12am and 10am.** Anything it wants to say overnight waits until morning, and goes out spread across the late morning rather than all at 10:00 on the dot. If you text it at night, it still replies.
- **It starts at most 3 texts a day** with any one person. Replying to you doesn't count, so you can go back and forth as much as you want. The Sunday recap doesn't count either. If it has more to say, the important stuff goes first and the rest waits a day.
- **It follows up, but it gives up.** If you don't reply, it nudges a couple of times and then moves on to someone else. No reply means no points, but no penalty either — you'll just show as "no response" in your weekly recap and be first in line next time.
- **If you go quiet for a while,** it'll check in once to see if you're OK or away. It won't assume anything. If you don't answer, nothing changes, and it won't ask again for at least two weeks.

---

## Things you can text it

You don't need commands. Write normally. These all work:

- "done" / "took out the trash"
- "busy this weekend, sorry"
- "remind me tomorrow" / "after my 3pm class"
- "did the toilet and sink, that's all I had time for"
- "trash is full" / "fridge is getting gross" (with or without a photo)
- "I already did the kitchen yesterday"
- "away until Sunday" — pauses you entirely, no asks while you're gone
- "I'm back" — ends a pause early
- "what are my points?"
- "I don't think the bathroom actually got done" — flags a claim
- **STOP** to opt out, **START** to come back. It only opts you out if the whole text is just that word, so "stop reminding me about trash" won't unsubscribe you.

---

## Fairness and trust

**Points are private.** You can see your own total, how you're doing against your share, and the group average, never anyone else's numbers. What got done and by whom is still visible, so it's clear the work happened.

**It assumes you're honest.** There's no proof required for anything. Photos are welcome but optional. If you send one, the bot checks that it shows the right spot and looks done, and asks for another if it doesn't. A photo never takes points away.

*The next two parts (flagging claims and photo requests) will be added after the first version is up and running.*

**If a claim seems off,** anyone can say so within a week. The points go on hold, and the bot asks the person what they got to — it won't say who raised it. Then:

- They send a photo → points restored
- They say they only did part of it → they get credit for that part, and the rest goes to someone else
- They say they didn't really do it → points dropped
- They stand by it but have no photo → points split in half
- No answer within two days → points dropped
- Whoever raised it can take it back at any time → points restored

The bot doesn't decide who's right. These rules do.

**If someone's claims keep getting dropped or split** (twice in a month), the bot quietly starts asking that person for a photo for the next month. Photo = full points; no photo = half. Nobody else is told.

**Pausing freezes you, it doesn't penalize you.** Go home for break, text it, and you come back level with everyone instead of buried. Back early? Just say so.

**Weekly recap, every Sunday evening,** sent to each of us separately. Shows your points next to the group average, whether you're ahead of or behind your share, what you did, what got done around the suite, and what's still undone. This is the main thing that keeps it fair with seven people — easy to assume someone else handled it otherwise.

**STOP** opts you out entirely, any time, no hard feelings. Your points freeze. **START** brings you back at the front of the line. Unlike a pause, you don't pick up where you left off.

---

## Open questions for the group

1. **Are the kitchen tiers right?** And should anything else be split by use — trash (a lot of it comes from cooking), or the bathrooms if some of us mostly use one?
2. **Are the rhythms right?** Trash every 3 days, dishes every 3–4, bathrooms weekends only, kitchen weekly, fridge every 2 weeks. Should bathrooms get a mid-week touch-up?
3. **Are the point weights fair?** Especially bathroom (3) versus kitchen (2), and the dishes split (0.25 vs 0.75).
4. **Is "no reply" handled right?** Right now it costs nothing but shows in your recap. Too soft? Too harsh?
5. **Is the dispute split fair?** If someone stands by a claim without a photo, they get half. Should it be less?
