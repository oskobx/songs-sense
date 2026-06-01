CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS songs (
    id               SERIAL PRIMARY KEY,
    artist           TEXT NOT NULL,
    featured_artists TEXT,
    title            TEXT NOT NULL,
    year             INTEGER,
    genre            TEXT,
    tier             TEXT NOT NULL,
    genius_id        INTEGER,
    lyrics           TEXT,
    lyrics_source    TEXT,
    created_at       TIMESTAMP DEFAULT NOW(),
    UNIQUE (artist, title)
);

CREATE INDEX IF NOT EXISTS songs_genius_id_idx ON songs (genius_id);

CREATE TABLE IF NOT EXISTS passages (
    id           SERIAL PRIMARY KEY,
    song_id      INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    passage_text TEXT NOT NULL,
    start_line   INTEGER,
    end_line     INTEGER,
    embedding    vector(768),
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS passages_song_id_idx ON passages (song_id);
