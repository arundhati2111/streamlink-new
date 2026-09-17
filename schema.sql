-- Schema for the down_station_automation table.
-- Matches the Result Grid columns/keys shown in the reference image.

CREATE TABLE `down_station_automation` (
  `id` int NOT NULL AUTO_INCREMENT,
  `st_id` int NOT NULL,
  `st_name` varchar(100) NOT NULL,
  `st_country` varchar(100) NOT NULL,
  `stream_link` varchar(512) NOT NULL,
  `new_stream_link` varchar(512) NOT NULL,
  `status` int NOT NULL DEFAULT '0',
  `source` varchar(100) DEFAULT NULL,
  `added_dt` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_dt` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `st_id` (`st_id`),
  KEY `stream_link` (`stream_link`),
  KEY `st_name` (`st_name`),
  KEY `source` (`source`)
) ENGINE=InnoDB AUTO_INCREMENT=10 DEFAULT CHARSET=utf8mb3;
