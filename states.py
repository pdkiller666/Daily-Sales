"""
Состояния для машины состояний FSM
"""
from aiogram.fsm.state import State, StatesGroup

class ProductStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_category = State()
    waiting_for_price = State()
    waiting_for_edit_choice = State()
    waiting_for_edit_product = State()
    waiting_for_edit_parameter = State()
    waiting_for_edit_value = State()
    waiting_for_category_rename = State()
    waiting_for_bulk_list = State()
    confirming_bulk_import = State()

class InventoryStates(StatesGroup):
    choosing_product = State()
    entering_quantity = State()
    adding_quantity = State()
    editing_quantity = State()
    waiting_for_new_quantity = State()

class SaleStates(StatesGroup):
    choosing_product = State()
    entering_quantity = State()
    entering_price = State()
    
class MultipleSaleStates(StatesGroup):
    adding_items = State()
    reviewing_cart = State()
    confirming_sale = State()

class EditSaleStates(StatesGroup):
    choosing_sale = State()
    editing_quantity = State()
    editing_price = State()
    editing_date = State()
    confirming_delete = State()
    choosing_start_date = State()
    choosing_end_date = State()

class ReportStates(StatesGroup):
    choosing_start_date = State()
    choosing_end_date = State()
    choosing_shop = State()
    choosing_city = State()

class UserRegistrationStates(StatesGroup):
    waiting_for_usage_mode = State()  # Личный, Создать, Войти
    waiting_for_invite_code = State() # Если вход по приглашению
    waiting_for_org_name = State()   # Если создание организации
    waiting_for_first_name = State()
    waiting_for_last_name = State()
    waiting_for_middle_name = State()
    waiting_for_phone = State()
    waiting_for_email = State()
    waiting_for_trade_network = State()
    waiting_for_shop_name = State()
    waiting_for_city = State()

class AdminUserStates(StatesGroup):
    selecting_user = State()
    editing_user = State()
    waiting_for_new_name = State()
    waiting_for_new_phone = State()
    waiting_for_new_email = State()
    waiting_for_new_network = State()
    waiting_for_new_shop = State()
    waiting_for_new_city = State()
    editing_timezone = State()
    waiting_for_admin_title = State()

class UserProfileStates(StatesGroup):
    editing_first_name = State()
    editing_last_name = State()
    editing_middle_name = State()
    editing_phone = State()
    editing_email = State()
    editing_network = State()
    editing_shop = State()
    editing_city = State()
    choosing_timezone = State()

class AdminManagementStates(StatesGroup):
    waiting_for_org_name = State()
    adding_admin = State()
    removing_admin = State()
    confirming_removal = State()
    waiting_for_admin_id = State()
    waiting_for_broadcast_message = State()

class SubscriptionStates(StatesGroup):
    waiting_payment_proof = State()
    waiting_for_promocode = State()

class NotificationStates(StatesGroup):
    waiting_for_threshold = State()
    waiting_for_time = State()
    waiting_for_admin_message = State()
    waiting_for_schedule_time = State()

class AdminNotificationStates(StatesGroup):
    choosing_recipients = State()
    selecting_users = State()
    entering_message = State()
    choosing_schedule = State()
    selecting_date = State()
    entering_time = State()
    confirming_send = State()

class PaymentSystemStates(StatesGroup):
    waiting_card_number = State()
    waiting_recipient_name = State()
    waiting_bank_name = State()
    waiting_plan_name = State()
    waiting_plan_price = State()
    waiting_plan_duration = State()
    waiting_plan_description = State()
    waiting_max_products = State()
    waiting_max_shops = State()
    waiting_max_sales = State()
    waiting_export_reports = State()
    waiting_analytics = State()
    waiting_notifications = State()
    waiting_promocode = State()
    waiting_discount = State()
    waiting_max_usage = State()
    waiting_user_search = State()
    selecting_plan_to_edit = State()
    editing_plan_field = State()
    editing_promocode_discount = State()
    editing_promocode_max_usage = State()
    waiting_payment_instruction = State()
    waiting_user_telegram_id = State()
    waiting_grant_user_id = State()
    waiting_extend_user_id = State()
    waiting_cancel_user_id = State()
    waiting_trial_days = State()
    waiting_trial_plan = State()
    waiting_yookassa_shop_id = State()
    waiting_yookassa_secret_key = State()
    waiting_yookassa_return_url = State()

class QuickSaleStates(StatesGroup):
    searching_product = State()

class SearchStates(StatesGroup):
    shop_commission    = State()   # commission_handlers — коэффициент смены
    user_catfilt       = State()   # commission_handlers — фильтр категорий
    shop_plans         = State()   # sales_plans_handlers — выбор магазина
    user_plans         = State()   # sales_plans_handlers — выбор продавца
    product_plans      = State()   # sales_plans_handlers — мультивыбор товаров
    category_plans     = State()   # sales_plans_handlers — мультивыбор категорий
    product_contests   = State()   # contests_handlers — товары конкурса
    category_contests  = State()   # contests_handlers — категории конкурса
    shop_reports       = State()   # reports_handlers — магазин отчёта за период
    shop_inventory     = State()   # inventory_handlers — магазин для остатков
    product_inventory  = State()   # inventory_handlers — товар для остатков
    shop_contacts      = State()   # contacts_handlers — магазин контактов

class ExcelImportStates(StatesGroup):
    waiting_file       = State()
    confirming_import  = State()

class RankingStates(StatesGroup):
    choosing_start_date = State()
    choosing_end_date   = State()