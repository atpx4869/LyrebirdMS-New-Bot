import pymysql

from app_config import config
from logger.logger import write_log

host = config['host']
port = config['port']
user = config['user']
password = config['password']
database = config['database']

connection = pymysql.connect(host=host, port=port, user=user, password=password, database=database)
try:
    with connection.cursor() as cursor:
        cursor.execute('UPDATE emby SET free = DEFAULT')
    connection.commit()
    write_log('每月免费额度已重置')
finally:
    connection.close()
