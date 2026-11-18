# Video donors: how they were picked (18.11.2026)

## Method

1. Apify `instagram-hashtag-scraper`, reels for 11 Russian meme hashtags (#мемы, #приколы, #юмор, #ржака,
   #смешноевидео, #мемасы, #жиза, #угар, #рофл, #приколыжизни, #смешно): 390 reels, 237 accounts.
2. Personal blogs dropped by hand, 30 meme pages kept.
3. Apify `instagram-reel-scraper`, last 12 reels of each: typical views (median of reels older than 48 h),
   posts per day, last post, Russian captions.

Cost: about $2.40 at $0.0026 per reel on the free plan.

## Picked

| Account | Group | Typical views | Posts/day | Last post | Best reel |
|---|---|---|---|---|---|
| hermesqz | memes | 229k | 0.2 | 15 days ago | 1.07M |
| hu1iganyo | memes | 20k | 0.1 | 1 day | 41k |
| ramzik.krsk24 | memes | 13k | 0.6 | 1 day | 472k |
| top.leym | memes | 12k | 0.1 | 1 day | 12.6M |
| daring_555 | memes | 11k | 0.2 | 1 day | 25k |
| axyennyi_tip | memes | 5k | 2.1 | today | 28k |
| meme.actually | memes | 3k | 0.5 | today | 420k |
| callmeayub_17 | movies | 37k | 1.5 | 10 days ago | 536k |

Rejected (under 3k typical views, inactive for months, or captions not in Russian): cchirians, krugliy.mem,
roma_memebrou, stopapupin, roflluntik0, andromixs, madara._.mem, hifanysh, _gidrant_1_, borovstroy877,
bezumnie.video, krepchai.mem, postironia.2026, toc4kaa, neirososed, genius.meme95, mos.smail, 4e6ypelllka,
kruzockimemm, fluystab, s1gma_memee, bmemesff, memeuted, posmeyemsya_yumor888, wildcut_25.

## Findings

- Russian meme pages on Instagram are weak: none posts daily with 100k+ typical views. Trends are one-off
  spikes (ramzik.krsk24: 13k typical, one reel 472k), which the "2x the donor median" rule is made to catch.
- The hashtag search cannot find fresh trends: Instagram shows only "top" reels for a hashtag, and Apify has
  no "recent" option. In a live run the freshest of 100 reels was 10 days old, the median about a year.
  So hashtags are useful only to discover new donors by hand, as above, not as a trend source.
- The film donor (callmeayub_17) often trips the violence words of the topic filter (its 536k reel did).
