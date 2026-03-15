CREATE TABLE IF NOT EXISTS download (
    id BIGSERIAL PRIMARY KEY,
    title TEXT,
    torrent_id TEXT,
    telegram_id TEXT,
    telegram_chat_id TEXT,
    cost_coins TEXT,
    size TEXT,
    date TIMESTAMP,
    tmdbid BIGINT
);

CREATE TABLE IF NOT EXISTS notified_transfers (
    id BIGINT PRIMARY KEY,
    notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
