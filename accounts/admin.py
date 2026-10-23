
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "role",
        "is_active",
        "is_staff",
    )

    list_filter = (
        "role",
        "is_active",
        "is_staff",
        "theme",
        "language",
    )

    search_fields = (
        "username",
        "email",
        "first_name",
        "last_name",
        "institution",
    )

    ordering = ("username",)

    fieldsets = UserAdmin.fieldsets + (
        ("HackFlow profile", {
            "fields": (
                "role",
                "phone",
                "institution",
                "field_of_study",
                "bio",
                "avatar",
            ),
        }),
        ("Preferences", {
            "fields": (
                "theme",
                "accent",
                "density",
                "language",
            ),
        }),
        ("Notifications", {
            "fields": (
                "notif_in_app",
                "notif_email",
                "notif_hackathon",
                "notif_team",
                "notif_deadlines",
                "notif_eval",
                "notif_prizes",
                "notif_weekly",
            ),
        }),
    )

    add_fieldsets = UserAdmin.add_fieldsets + (
        ("HackFlow account", {
            "classes": ("wide",),
            "fields": ("email", "role"),
        }),
    )
