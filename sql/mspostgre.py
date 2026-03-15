import psycopg2

from app_config import config
from logger.logger import write_log

mspostgre_host = config['mspostgre_host']
mspostgre_port = config['mspostgre_port']
mspostgre_dbname = config['mspostgre_dbname']
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


def get_torrent_info_by_id(torrent_id):
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM torrent_info WHERE id = %s', (str(torrent_id),))
            row = cur.fetchone()
            if not row:
                return None
            colnames = [desc[0] for desc in cur.description]
            return dict(zip(colnames, row))
    except Exception as e:
        write_log(f'查询 torrent_info 失败 id={torrent_id}: {e}', level='ERROR')
        return None
    finally:
        if conn:
            conn.close()
