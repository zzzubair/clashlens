-- Clash Lens deployment migration 0089.
-- Unit catalog v3: Minion Prince equipment 61 is Portal Pendant, released
-- 2026-10-08. It was missing from the catalog, so armies carrying it were
-- marked partial and left out of exact-army statistics. v2 armies stay as
-- history, but pages only read the catalog the running code pins, so this
-- queues re-decoding of the current Season's saved battles the re-decode job
-- can safely take (the skipped ones are listed below). `up` runs it while
-- services are stopped, so no worker on the old catalog can claim these jobs.
-- A repeat run adds no job, and re-decoding a battle already on v3 writes
-- nothing. Each re-decoded army adds a new row of about 2.2 KB and keeps the
-- old one as history: 378,123 battles in 3,782 jobs on 2026-10-09, up to
-- about 1.7 GB once, plus the rebuilt day statistics. The migration only adds
-- the job rows (choosing the battles took 7.6 seconds on production); the
-- worker re-decodes them after the deploy, in the backfill class below Reset
-- and live work.
BEGIN;

INSERT INTO unit_catalog_versions (version, content_hash, provenance, license, entries)
VALUES (
    'unit-catalog-v3',
    'bf50934544103db44dc97af8ac41c9ca827a0acf2ffa2830006e8911d89ce4c5',
    'ClashKingInc/clashy.py@0703aee64a24c48aef296856bd688704d434181f coc/static/static_data.json blob b90a6b2bfbccac3b755a68f78fe8885b35bc80d6 sha256 3fa1e2b9ccd4a24f48ca7ade5a23d54c8ce0c3f17ad986270f2b913584f9c1d5; fixture h0p9e14_32d1x53u2x58-1x97s2x2 observed 2026-08-21; v2 adds equipment:60 Revenge Deck (Dragon Duke equipment 60 matched Revenge Deck in archived player profiles, 2026-10-03) and names seasonal troop:167 Meteor Golem (event), which the same static data separates from Barracks troop:177; v3 adds equipment:61 Portal Pendant (Minion Prince equipment 61 first seen in battle logs 2026-10-08 08:03 UTC and matched Portal Pendant in archived player profiles, 2026-10-09; Zacatac3/clash_widgets@4f12eab15671e305e8b4df1b5e4b11025437cfac clash_widgets/json/mapping.json maps 90000061 to Portal Pendant)',
    'MIT; Supercell Fan Content Policy applies to game metadata',
    '{"equipment:0":{"category":"equipment","is_siege":false,"name":"Barbarian Puppet"},"equipment:1":{"category":"equipment","is_siege":false,"name":"Rage Vial"},"equipment:10":{"category":"equipment","is_siege":false,"name":"Giant Gauntlet"},"equipment:11":{"category":"equipment","is_siege":false,"name":"Vampstache"},"equipment:12":{"category":"equipment","is_siege":false,"name":"Haste Vial"},"equipment:13":{"category":"equipment","is_siege":false,"name":"Rocket Spear"},"equipment:14":{"category":"equipment","is_siege":false,"name":"Spiky Ball"},"equipment:15":{"category":"equipment","is_siege":false,"name":"Frozen Arrow"},"equipment:16":{"category":"equipment","is_siege":false,"name":"Monolith Arrow"},"equipment:17":{"category":"equipment","is_siege":false,"name":"Giant Arrow"},"equipment:19":{"category":"equipment","is_siege":false,"name":"Heroic Torch"},"equipment:2":{"category":"equipment","is_siege":false,"name":"Archer Puppet"},"equipment:20":{"category":"equipment","is_siege":false,"name":"Healer Puppet"},"equipment:22":{"category":"equipment","is_siege":false,"name":"Fireball"},"equipment:24":{"category":"equipment","is_siege":false,"name":"Rage Gem"},"equipment:3":{"category":"equipment","is_siege":false,"name":"Invisibility Vial"},"equipment:32":{"category":"equipment","is_siege":false,"name":"Snake Bracelet"},"equipment:34":{"category":"equipment","is_siege":false,"name":"Healing Tome"},"equipment:35":{"category":"equipment","is_siege":false,"name":"Dark Crown"},"equipment:39":{"category":"equipment","is_siege":false,"name":"Magic Mirror"},"equipment:4":{"category":"equipment","is_siege":false,"name":"Eternal Tome"},"equipment:40":{"category":"equipment","is_siege":false,"name":"Electro Boots"},"equipment:41":{"category":"equipment","is_siege":false,"name":"Lavaloon Puppet"},"equipment:42":{"category":"equipment","is_siege":false,"name":"Henchmen Puppet"},"equipment:43":{"category":"equipment","is_siege":false,"name":"Dark Orb"},"equipment:44":{"category":"equipment","is_siege":false,"name":"Metal Pants"},"equipment:47":{"category":"equipment","is_siege":false,"name":"Noble Iron"},"equipment:48":{"category":"equipment","is_siege":false,"name":"Action Figure"},"equipment:49":{"category":"equipment","is_siege":false,"name":"Meteor Staff"},"equipment:5":{"category":"equipment","is_siege":false,"name":"Life Gem"},"equipment:50":{"category":"equipment","is_siege":false,"name":"Frost Flake"},"equipment:51":{"category":"equipment","is_siege":false,"name":"Stick Horse"},"equipment:52":{"category":"equipment","is_siege":false,"name":"Fire Heart"},"equipment:53":{"category":"equipment","is_siege":false,"name":"Rocket Backpack"},"equipment:56":{"category":"equipment","is_siege":false,"name":"Stun Blaster"},"equipment:57":{"category":"equipment","is_siege":false,"name":"Flame Blower"},"equipment:59":{"category":"equipment","is_siege":false,"name":"Electro Fangs"},"equipment:6":{"category":"equipment","is_siege":false,"name":"Seeking Shield"},"equipment:60":{"category":"equipment","is_siege":false,"name":"Revenge Deck"},"equipment:61":{"category":"equipment","is_siege":false,"name":"Portal Pendant"},"equipment:7":{"category":"equipment","is_siege":false,"name":"Royal Gem"},"equipment:8":{"category":"equipment","is_siege":false,"name":"Earthquake Boots"},"equipment:9":{"category":"equipment","is_siege":false,"name":"Hog Rider Puppet"},"hero:0":{"category":"hero","is_siege":false,"name":"Barbarian King"},"hero:1":{"category":"hero","is_siege":false,"name":"Archer Queen"},"hero:2":{"category":"hero","is_siege":false,"name":"Grand Warden"},"hero:4":{"category":"hero","is_siege":false,"name":"Royal Champion"},"hero:6":{"category":"hero","is_siege":false,"name":"Minion Prince"},"hero:7":{"category":"hero","is_siege":false,"name":"Dragon Duke"},"pet:0":{"category":"pet","is_siege":false,"name":"L.A.S.S.I"},"pet:1":{"category":"pet","is_siege":false,"name":"Mighty Yak"},"pet:10":{"category":"pet","is_siege":false,"name":"Spirit Fox"},"pet:11":{"category":"pet","is_siege":false,"name":"Angry Jelly"},"pet:16":{"category":"pet","is_siege":false,"name":"Sneezy"},"pet:17":{"category":"pet","is_siege":false,"name":"Greedy Raven"},"pet:2":{"category":"pet","is_siege":false,"name":"Electro Owl"},"pet:3":{"category":"pet","is_siege":false,"name":"Unicorn"},"pet:4":{"category":"pet","is_siege":false,"name":"Phoenix"},"pet:7":{"category":"pet","is_siege":false,"name":"Poison Lizard"},"pet:8":{"category":"pet","is_siege":false,"name":"Diggy"},"pet:9":{"category":"pet","is_siege":false,"name":"Frosty"},"spell:0":{"category":"spell","is_siege":false,"name":"Lightning Spell"},"spell:1":{"category":"spell","is_siege":false,"name":"Healing Spell"},"spell:10":{"category":"spell","is_siege":false,"name":"Earthquake Spell"},"spell:109":{"category":"spell","is_siege":false,"name":"Ice Block Spell"},"spell:11":{"category":"spell","is_siege":false,"name":"Haste Spell"},"spell:120":{"category":"spell","is_siege":false,"name":"Totem Spell"},"spell:123":{"category":"spell","is_siege":false,"name":"Angry Spell"},"spell:16":{"category":"spell","is_siege":false,"name":"Clone Spell"},"spell:17":{"category":"spell","is_siege":false,"name":"Skeleton Spell"},"spell:2":{"category":"spell","is_siege":false,"name":"Rage Spell"},"spell:28":{"category":"spell","is_siege":false,"name":"Bat Spell"},"spell:3":{"category":"spell","is_siege":false,"name":"Jump Spell"},"spell:35":{"category":"spell","is_siege":false,"name":"Invisibility Spell"},"spell:5":{"category":"spell","is_siege":false,"name":"Freeze Spell"},"spell:53":{"category":"spell","is_siege":false,"name":"Recall Spell"},"spell:6":{"category":"spell","is_siege":false,"name":"Santa''s Surprise"},"spell:70":{"category":"spell","is_siege":false,"name":"Overgrowth Spell"},"spell:73":{"category":"spell","is_siege":false,"name":"Bag of Frostmites"},"spell:9":{"category":"spell","is_siege":false,"name":"Poison Spell"},"spell:98":{"category":"spell","is_siege":false,"name":"Revive Spell"},"troop:0":{"category":"troop","is_siege":false,"name":"Barbarian"},"troop:1":{"category":"troop","is_siege":false,"name":"Archer"},"troop:10":{"category":"troop","is_siege":false,"name":"Minion"},"troop:101":{"category":"troop","is_siege":false,"name":"Barcher"},"troop:102":{"category":"troop","is_siege":false,"name":"Witch Golem"},"troop:103":{"category":"troop","is_siege":false,"name":"Hog Wizard"},"troop:104":{"category":"troop","is_siege":false,"name":"Lavaloon"},"troop:109":{"category":"troop","is_siege":false,"name":"Ruin Witch"},"troop:11":{"category":"troop","is_siege":false,"name":"Hog Rider"},"troop:110":{"category":"troop","is_siege":false,"name":"Root Rider"},"troop:118":{"category":"troop","is_siege":false,"name":"C.O.O.K.I.E"},"troop:119":{"category":"troop","is_siege":false,"name":"Firecracker"},"troop:12":{"category":"troop","is_siege":false,"name":"Valkyrie"},"troop:120":{"category":"troop","is_siege":false,"name":"Azure Dragon"},"troop:121":{"category":"troop","is_siege":false,"name":"Barbarian Kicker"},"troop:122":{"category":"troop","is_siege":false,"name":"Giant Thrower"},"troop:123":{"category":"troop","is_siege":false,"name":"Druid"},"troop:125":{"category":"troop","is_siege":false,"name":"Broom Witch"},"troop:13":{"category":"troop","is_siege":false,"name":"Golem"},"troop:130":{"category":"troop","is_siege":false,"name":"Ice Minion"},"troop:132":{"category":"troop","is_siege":false,"name":"Thrower"},"troop:135":{"category":"troop","is_siege":true,"name":"Troop Launcher"},"troop:136":{"category":"troop","is_siege":false,"name":"Debt Collector"},"troop:142":{"category":"troop","is_siege":false,"name":"Snake Barrel"},"troop:147":{"category":"troop","is_siege":false,"name":"Super Yeti"},"troop:15":{"category":"troop","is_siege":false,"name":"Witch"},"troop:150":{"category":"troop","is_siege":false,"name":"Furnace"},"troop:156":{"category":"troop","is_siege":false,"name":"Giant Giant"},"troop:157":{"category":"troop","is_siege":false,"name":"K.A.N.E"},"troop:158":{"category":"troop","is_siege":false,"name":"The Disarmer"},"troop:159":{"category":"troop","is_siege":false,"name":"YEETer"},"troop:167":{"category":"troop","is_siege":false,"name":"Meteor Golem (event)"},"troop:17":{"category":"troop","is_siege":false,"name":"Lava Hound"},"troop:177":{"category":"troop","is_siege":false,"name":"Meteor Golem"},"troop:188":{"category":"troop","is_siege":true,"name":"Sky Wagon"},"troop:2":{"category":"troop","is_siege":false,"name":"Goblin"},"troop:22":{"category":"troop","is_siege":false,"name":"Bowler"},"troop:23":{"category":"troop","is_siege":false,"name":"Baby Dragon"},"troop:24":{"category":"troop","is_siege":false,"name":"Miner"},"troop:26":{"category":"troop","is_siege":false,"name":"Super Barbarian"},"troop:27":{"category":"troop","is_siege":false,"name":"Super Archer"},"troop:28":{"category":"troop","is_siege":false,"name":"Super Wall Breaker"},"troop:29":{"category":"troop","is_siege":false,"name":"Super Giant"},"troop:3":{"category":"troop","is_siege":false,"name":"Giant"},"troop:30":{"category":"troop","is_siege":false,"name":"Ice Wizard"},"troop:4":{"category":"troop","is_siege":false,"name":"Wall Breaker"},"troop:45":{"category":"troop","is_siege":false,"name":"Battle Ram"},"troop:47":{"category":"troop","is_siege":false,"name":"Royal Ghost"},"troop:48":{"category":"troop","is_siege":false,"name":"Pumpkin Barbarian"},"troop:5":{"category":"troop","is_siege":false,"name":"Balloon"},"troop:50":{"category":"troop","is_siege":false,"name":"Giant Skeleton"},"troop:51":{"category":"troop","is_siege":true,"name":"Wall Wrecker"},"troop:52":{"category":"troop","is_siege":true,"name":"Battle Blimp"},"troop:53":{"category":"troop","is_siege":false,"name":"Yeti"},"troop:55":{"category":"troop","is_siege":false,"name":"Sneaky Goblin"},"troop:56":{"category":"troop","is_siege":false,"name":"Super Miner"},"troop:57":{"category":"troop","is_siege":false,"name":"Rocket Balloon"},"troop:58":{"category":"troop","is_siege":false,"name":"Ice Golem"},"troop:59":{"category":"troop","is_siege":false,"name":"Electro Dragon"},"troop:6":{"category":"troop","is_siege":false,"name":"Wizard"},"troop:61":{"category":"troop","is_siege":false,"name":"Skeleton Barrel"},"troop:62":{"category":"troop","is_siege":true,"name":"Stone Slammer"},"troop:63":{"category":"troop","is_siege":false,"name":"Inferno Dragon"},"troop:64":{"category":"troop","is_siege":false,"name":"Super Valkyrie"},"troop:65":{"category":"troop","is_siege":false,"name":"Dragon Rider"},"troop:66":{"category":"troop","is_siege":false,"name":"Super Witch"},"troop:67":{"category":"troop","is_siege":false,"name":"M.E.C.H.A"},"troop:7":{"category":"troop","is_siege":false,"name":"Healer"},"troop:72":{"category":"troop","is_siege":false,"name":"Party Wizard"},"troop:75":{"category":"troop","is_siege":true,"name":"Siege Barracks"},"troop:76":{"category":"troop","is_siege":false,"name":"Ice Hound"},"troop:8":{"category":"troop","is_siege":false,"name":"Dragon"},"troop:80":{"category":"troop","is_siege":false,"name":"Super Bowler"},"troop:81":{"category":"troop","is_siege":false,"name":"Super Dragon"},"troop:82":{"category":"troop","is_siege":false,"name":"Headhunter"},"troop:83":{"category":"troop","is_siege":false,"name":"Super Wizard"},"troop:84":{"category":"troop","is_siege":false,"name":"Super Minion"},"troop:87":{"category":"troop","is_siege":true,"name":"Log Launcher"},"troop:9":{"category":"troop","is_siege":false,"name":"P.E.K.K.A"},"troop:91":{"category":"troop","is_siege":true,"name":"Flame Flinger"},"troop:92":{"category":"troop","is_siege":true,"name":"Battle Drill"},"troop:94":{"category":"troop","is_siege":false,"name":"Ram Rider"},"troop:95":{"category":"troop","is_siege":false,"name":"Electro Titan"},"troop:97":{"category":"troop","is_siege":false,"name":"Apprentice Warden"},"troop:98":{"category":"troop","is_siege":false,"name":"Super Hog Rider"}}'
)
ON CONFLICT (version) DO NOTHING;

-- Only battles from the current Season's Day 1 (2026-10-05 05:00 UTC) on:
-- each Reset's publication reads the whole Season's armies under the catalog
-- the running code pins, so a Season half on v2 would lose its earlier days'
-- armies. Earlier Seasons are already published and stay on v2 as history.
-- Batches of 100 battles in the backfill class, below live work. Newer
-- battles fall due first so the days still waiting on Reset publication are
-- re-decoded before older ones. Battles on a day without a Legend-day record
-- are skipped: the re-decode job cannot place them in a Season. Battles in a
-- finalized or retired Season are skipped: the job refuses any batch that
-- holds one. Battles on a completed day with no current Reset publication
-- record are skipped: re-decoding them would queue statistics work the
-- publication guard rejects, rolling back the whole batch. Those historical
-- days are not repaired here; repairing them is a possible follow-up.
-- Each condition depends only on the battle's day, so it is checked once per
-- day: checked per battle it took 12 minutes 41 seconds on production on
-- 2026-10-03.
WITH days AS (
    SELECT day.ranked_day_start
    FROM (SELECT DISTINCT ranked_day_start FROM legend_battles) AS day
    WHERE day.ranked_day_start >= '2026-10-05 05:00:00+00'
    AND EXISTS (
        SELECT 1 FROM ranked_day_versions AS ranked
        WHERE ranked.ranked_day_start = day.ranked_day_start
    )
    AND NOT EXISTS (
        SELECT 1 FROM ranked_day_versions AS ranked
        JOIN season_detail_retirements AS retirement
          ON retirement.official_season_id = ranked.official_season_id
        WHERE ranked.ranked_day_start = day.ranked_day_start
          AND retirement.status IN ('finalized', 'retired')
    )
    AND NOT EXISTS (
        SELECT 1 FROM ranked_day_versions AS ranked
        WHERE ranked.ranked_day_start = day.ranked_day_start
          AND ranked.state = 'Complete'
          AND ranked.coverage_complete
          AND NOT EXISTS (
              SELECT 1 FROM boundary_publication_generations AS generation
              WHERE generation.boundary_at = day.ranked_day_start + interval '24 hours'
                AND generation.snapshot_state <> 'superseded'
                AND generation.army_state <> 'superseded'
          )
    )
), numbered AS (
    SELECT id, (row_number() OVER (ORDER BY id) - 1) / 100 AS batch
    FROM legend_battles AS battle JOIN days USING (ranked_day_start)
), batches AS (
    SELECT batch, jsonb_agg(id ORDER BY id) AS battle_ids,
           min(id) AS first_id, max(id) AS last_id
    FROM numbered GROUP BY batch
)
INSERT INTO python_processing_jobs (
    work_type, deduplication_key, input_json, priority, processing_version,
    domain_rule_version, analytics_rule_version, due_at
)
SELECT 'redecode_army',
       format('redecode_army:army-decoder-v2:unit-catalog-v3:%s:%s', first_id, last_id),
       jsonb_build_object('battle_ids', battle_ids),
       25, 'clashlens-domain-processing-v1', 'clashlens-domain-rules-v1',
       'army-analytics-v2', clock_timestamp() - make_interval(secs => batch)
FROM batches ON CONFLICT (deduplication_key) DO NOTHING;

INSERT INTO clash_lens_schema_migrations(version) VALUES (89)
ON CONFLICT (version) DO NOTHING;
COMMIT;
