from django.contrib import admin

from .models import Notification, NotificationDelivery, NotificationPreference, QuietHours


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "event_type", "is_read", "created_at")
    list_filter = ("event_type", "is_read", "created_at")
    search_fields = ("title", "body", "user__email")
    readonly_fields = ("id", "created_at")
    raw_id_fields = ("user",)


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "event_type", "channel", "is_enabled")
    list_filter = ("event_type", "channel", "is_enabled")
    raw_id_fields = ("user",)


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "notification",
        "channel",
        "status",
        "attempts",
        "batch_queued_at",
        "batch_claimed_at",
        "delivered_at",
    )
    list_filter = ("channel", "status", "batch_queued_at")
    # The batch columns are the digest sweep's bookkeeping: editing a claim by
    # hand either strands the row or hands the same batch to a second sweep.
    readonly_fields = ("id", "created_at", "batch_queued_at", "batch_claimed_at")


@admin.register(QuietHours)
class QuietHoursAdmin(admin.ModelAdmin):
    list_display = ("user", "is_enabled", "start_time", "end_time", "timezone", "digest_mode")
    raw_id_fields = ("user",)
