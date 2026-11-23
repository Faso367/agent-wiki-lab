"""Five independent changes; numbers and gold live outside the indexed source text."""
from wiki import add_page, connect

# service, RU title, setting, new value, previous value, units, EN approval, RU search prompt
CASES = [
 ("atlas","Доставка событий","Количество повторных попыток",2,5,"попытки",
  "EU webhook delivery is capped at 2 retries after initial failure.",
  "Сколько раз повторять доставку событий в регионе EU?"),
 ("lumen","Хранение выгрузок","Срок хранения",14,30,"дней",
  "EU daily exports must expire after 14 days.",
  "Когда удалять ежедневные выгрузки в регионе EU?"),
 ("orbit","Тайм-аут запроса","Время ожидания ответа",45,90,"секунд",
  "EU gateway requests must time out after 45 seconds.",
  "Сколько ждать ответа шлюза в регионе EU?"),
 ("mira","Пакетная загрузка","Максимальный размер пакета",80,100,"записей",
  "EU ingestion batches accept at most 80 records.",
  "Сколько записей помещается в пакет для региона EU?"),
 ("vega","Частота опроса","Интервал опроса",12,20,"минут",
  "EU health probes must be scheduled every 12 minutes.",
  "Как часто запускать проверки состояния в регионе EU?")
]


def seed(path):
    with connect(path,True) as db:
        if db.execute("SELECT 1 FROM imports WHERE name='fixtures-v1'").fetchone():
            return
        for key,title,setting,new,old,units,fact,_ in CASES:
            add_page(db,key+"-decision",key.title()+" | Approved EU decision","en",
                     [("Approved policy",fact),("Applicability","Only applies to customer-facing EU production.")])
            add_page(db,key+"-draft",key.title()+" | US proposal","en",
                     [("Draft",f"Unapproved proposal, US only. Suggested value: {old}.")])
            add_page(db,key+"-guide",key.title()+" · "+title,"ru",
                     [(setting,f"Для EU действует значение {old} ({units})."),
                      ("Эскалация","При расхождении с договором обратитесь к дежурному инженеру.")])
        db.execute("INSERT INTO imports VALUES('fixtures-v1')")
