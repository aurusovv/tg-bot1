import os
os.environ['TOKEN'] = '8608054971:AAEWfrZNqG-TXSyu1Udnvy7bZWEufuX807k'
os.environ['DATABASE_URL'] = 'postgresql://anocolos:Xw8-NVk-4Cd-xru@postgresql-anocolos.alwaysdata.net/anocolos_mybot_db'
from bot_telegram import flask_app as application
