import pytest
from unittest.mock import MagicMock, patch
from core.orchestrator import Orchestrator

@pytest.fixture
def mock_nocodb_clients():
    """Автоматически мокает клиенты NocoDB."""
    with patch('core.nocodb.NocoDBClient') as mock_nocodb, \
         patch('core.nocodb.ProjectsClient') as mock_projects, \
         patch('core.nocodb.TasksClient') as mock_tasks:
        
        yield {
            'nocodb': mock_nocodb.return_value,
            'projects': mock_projects.return_value,
            'tasks': mock_tasks.return_value
        }

@pytest.fixture
def orchestrator(mock_nocodb_clients):
    """Создаёт Orchestrator с внедрёнными моками."""
    orch = Orchestrator()
    # ⭐ ВАЖНО: Принудительно подменяем клиенты на моки
    orch.projects_db = mock_nocodb_clients['projects']
    orch.tasks_db = mock_nocodb_clients['tasks']
    orch.agent_logs_db = mock_nocodb_clients['nocodb']
    return orch