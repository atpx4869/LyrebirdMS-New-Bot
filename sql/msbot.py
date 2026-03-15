import psycopg2

from app_config import config
from logger.logger import write_log

mspostgre_host = config['mspostgre_host']
mspostgre_port = config['mspostgre_port']
mspostgre_dbname = config.get('mspostgre_dbname', 'ms-bot')
mspostgre_user = config['mspostgre_user']
mspostgre_password = config['mspostgre_password']


def _connect():
    return psycopg2.connect(
        host=mspostgre_host,
        port=mspostgre_port,
        dbname=mspostgre_dbname,
        user=mspostgre_user,
        password=mspostgre_password,
        connect_timeout=10,
    )


def create_download_table():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                '''
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
                )
                '''
            )
        conn.commit()
    finally:
        conn.close()


def insert_download_data(title, torrent_id, telegram_id, telegram_chat_id, cost_coins, size, date, tmdbid):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO download (title, torrent_id, telegram_id, telegram_chat_id, cost_coins, size, date, tmdbid) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                (title, torrent_id, telegram_id, telegram_chat_id, cost_coins, size, date, tmdbid),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        write_log(f'写入下载记录失败 torrent_id={torrent_id}: {e}', level='ERROR')
        raise
    finally:
        conn.close()


def create_notified_transfers_table():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                '''
                CREATE TABLE IF NOT EXISTS notified_transfers (
                    id BIGINT PRIMARY KEY,
                    notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                '''
            )
        conn.commit()
        create_download_table()
    finally:
        conn.close()


def is_transfer_notified(transfer_id):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT 1 FROM notified_transfers WHERE id = %s', (transfer_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def mark_transfer_notified(transfer_id):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO notified_transfers (id) VALUES (%s) ON CONFLICT (id) DO NOTHING', (transfer_id,))
        conn.commit()
    finally:
        conn.close()


def get_recent_downloads_by_tmdbid(tmdbid):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM download WHERE tmdbid = %s AND date >= NOW() - INTERVAL '2 days'",
                (tmdbid,),
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def list_recent_downloads(limit=50):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT title, torrent_id, telegram_id, telegram_chat_id, cost_coins, size, date, tmdbid FROM download ORDER BY date DESC LIMIT %s',
                (limit,),
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception as e:
        write_log(f'查询最近下载记录失败: {e}', level='ERROR')
        return []
    finally:
        conn.close()


def get_download_stats():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM download')
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM download WHERE date >= NOW() - INTERVAL '1 day'")
            today = cur.fetchone()[0]
            return {'total_downloads': total, 'last_24h_downloads': today}
    except Exception as e:
        write_log(f'查询下载统计失败: {e}', level='ERROR')
        return {'total_downloads': 0, 'last_24h_downloads': 0}
    finally:
        conn.close()


def search_downloads(query='', limit=100):
    conn = _connect()
    try:
        like = f'%{query}%'
        with conn.cursor() as cur:
            cur.execute(
                'SELECT title, torrent_id, telegram_id, telegram_chat_id, cost_coins, size, date, tmdbid FROM download WHERE (%s = '' OR title ILIKE %s OR torrent_id ILIKE %s OR telegram_id::text ILIKE %s) ORDER BY date DESC LIMIT %s',
                (query, like, like, like, limit),
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception as e:
        write_log(f'搜索下载记录失败 query={query}: {e}', level='ERROR')
        return []
    finally:
        conn.close()


def get_download_by_torrent_id(torrent_id):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT title, torrent_id, telegram_id, telegram_chat_id, cost_coins, size, date, tmdbid FROM download WHERE torrent_id = %s ORDER BY date DESC LIMIT 1',
                (torrent_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row))
    except Exception as e:
        write_log(f'查询下载详情失败 torrent_id={torrent_id}: {e}', level='ERROR')
        return None
    finally:
        conn.close()


def get_downloads_by_user(telegram_id, limit=50):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT title, torrent_id, telegram_id, telegram_chat_id, cost_coins, size, date, tmdbid FROM download WHERE telegram_id::text = %s ORDER BY date DESC LIMIT %s',
                (str(telegram_id), limit),
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception as e:
        write_log(f'查询用户下载记录失败 tg={telegram_id}: {e}', level='ERROR')
        return []
    finally:
        conn.close()
