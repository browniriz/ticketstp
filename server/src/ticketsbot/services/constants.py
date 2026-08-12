TICKET_TYPES = ["Ломбард", "Скупка", "Касса", "Ошибка", "Перемещение", "Оприходование", "Изъятие", "Списание", "Возвраты клиентам"]
ADMIN_ONLY_TICKET_TYPES = {"Списание"}
DEFAULT_EMPLOYEE_TICKET_TYPES = ["Ломбард", "Скупка", "Касса", "Ошибка", "Изъятие"]

NEW = "создана"
WORK = "в работе"
PAUSE = "на паузе"
DONE = "решена"
REVISION = "на доработке"
FIXED = "исправлена"
REJECTED = "отклонена"
TERMINAL = {DONE, REJECTED}
