from aiogram.fsm.state import State, StatesGroup


class Checkout(StatesGroup):
    saved_profile = State()
    recipient_full_name = State()
    phone = State()
    delivery_method = State()
    postal_code = State()
    region = State()
    city = State()
    cdek_type = State()
    cdek_point = State()
    street = State()
    house = State()
    building = State()
    apartment = State()
    delivery_comment = State()
    save_profile = State()
    promo = State()
    points = State()
    confirm = State()


class ReceiptUpload(StatesGroup):
    waiting = State()


class ReviewFlow(StatesGroup):
    rating = State()
    text = State()


class AdminAddProduct(StatesGroup):
    category = State()
    name = State()
    price = State()
    description = State()
    weight = State()
    color_mode = State()
    single_color = State()
    variants = State()
    photo = State()


class AdminEditValue(StatesGroup):
    value = State()


class AdminBroadcast(StatesGroup):
    waiting_message = State()
    confirm = State()
    sending = State()


class AdminTracking(StatesGroup):
    waiting_track = State()


class AdminPromo(StatesGroup):
    code = State()
    percent = State()
    min_order = State()
    max_uses = State()


class AdminNote(StatesGroup):
    text = State()


class AdminVariantEdit(StatesGroup):
    text = State()


class AdminAddPhoto(StatesGroup):
    color = State()
    photo = State()


class AdminAddAdmin(StatesGroup):
    user_id = State()
    role = State()


class AdminReviewLink(StatesGroup):
    url = State()


class AdminBonus(StatesGroup):
    amount = State()


class AdminSizeChart(StatesGroup):
    text = State()


class AdminContentEdit(StatesGroup):
    text = State()
    media = State()

class AdminButtonEdit(StatesGroup):
    text = State()
    emoji = State()


class AdminPremiumEmoji(StatesGroup):
    waiting = State()
    pack_link = State()
    search_query = State()
    placement_target = State()
    global_replace_target = State()


class AdminRequiredChannel(StatesGroup):
    channel = State()



class AdminCategoryEdit(StatesGroup):
    name = State()
    media = State()

class AdminGlobalSearch(StatesGroup):
    query = State()


class AdminPremiumPreset(StatesGroup):
    confirm = State()
