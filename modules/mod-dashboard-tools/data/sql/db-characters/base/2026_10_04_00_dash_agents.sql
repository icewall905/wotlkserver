-- Tables for ac-manager's LLM agents (mod-dashboard-tools). Safe to re-run.
CREATE TABLE IF NOT EXISTS `dash_agents` (
  `guid` INT UNSIGNED NOT NULL PRIMARY KEY,
  `name` VARCHAR(12) NOT NULL,
  `active` TINYINT NOT NULL DEFAULT 1,
  `paused` TINYINT NOT NULL DEFAULT 0,
  `sleeping` TINYINT NOT NULL DEFAULT 0,
  `break_until` DATETIME NULL,
  `persona` TEXT NULL,
  `goal` TEXT NULL,
  `nudge` TEXT NULL,
  `last_thought` TEXT NULL,
  `last_action` VARCHAR(255) NULL,
  `last_think` DATETIME NULL,
  `born_at` DATETIME NULL,
  `born_played` INT UNSIGNED NULL COMMENT 'played time (s) when this life began',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LLM agents driven by ac-manager';

CREATE TABLE IF NOT EXISTS `dash_agent_events` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `guid` INT UNSIGNED NOT NULL,
  `ts` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `kind` VARCHAR(16) NOT NULL,
  `level` TINYINT UNSIGNED NULL,
  `zone` VARCHAR(64) NULL,
  `text` TEXT NOT NULL,
  KEY `idx_guid_ts` (`guid`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `dash_agent_memories` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `guid` INT UNSIGNED NOT NULL,
  `ts` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `importance` TINYINT UNSIGNED NOT NULL DEFAULT 5,
  `about` VARCHAR(12) NULL,
  `text` TEXT NOT NULL,
  KEY `idx_guid` (`guid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `dash_settings` (
  `k` VARCHAR(64) NOT NULL PRIMARY KEY,
  `v` VARCHAR(255) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO `dash_settings` (`k`, `v`) VALUES ('agents_enabled', '1');
