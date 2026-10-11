from django.shortcuts import render


ROLE_DASHBOARDS = {
    'organizer': 'organizer_dashboard.html',
    'jury': 'jury_dashboard.html',
    'participant': 'participant_dashboard.html',
}


def dashboard(request):
    template = ROLE_DASHBOARDS.get(getattr(request.user, 'role', None), 'dashboard.html')
    return render(request, template)

def hackathons(request):
    return render(request, "hackathons.html")

def projects(request):
    return render(request, "projects.html")

def teams(request):
    return render(request, "teams.html")


def evaluations(request):
    return render(request, "evaluations.html")