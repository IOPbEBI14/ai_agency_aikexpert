"""Инварианты Task Graph: sales обязателен после hunter."""
from core.task_graph_rules import enforce_sales_after_hunters, prefer_single_hunter


class TestEnforceSalesAfterHunters:
    def test_adds_sales_when_client_hunter_only(self):
        tasks, excluded = enforce_sales_after_hunters(
            [
                {
                    "task_id": "task_001",
                    "agent_name": "client_hunter",
                    "task_description": "Найти клиники",
                    "depends_on": [],
                }
            ],
            excluded_agents=["sales", "developer"],
        )
        agents = [t["agent_name"] for t in tasks]
        assert "sales" in agents
        assert "sales" not in excluded
        assert "developer" in excluded
        sales = next(t for t in tasks if t["agent_name"] == "sales")
        assert "task_001" in sales["depends_on"]

    def test_wires_existing_sales_depends_on(self):
        tasks, _ = enforce_sales_after_hunters(
            [
                {
                    "task_id": "ch1",
                    "agent_name": "client_hunter",
                    "depends_on": [],
                },
                {
                    "task_id": "s1",
                    "agent_name": "sales",
                    "depends_on": [],
                    "task_description": "Написать сообщения",
                },
            ]
        )
        sales = next(t for t in tasks if t["task_id"] == "s1")
        assert sales["depends_on"] == ["ch1"]

    def test_noop_without_hunters(self):
        original = [
            {
                "task_id": "a1",
                "agent_name": "analyst",
                "depends_on": [],
            }
        ]
        tasks, excluded = enforce_sales_after_hunters(
            original, excluded_agents=["sales"]
        )
        assert [t["agent_name"] for t in tasks] == ["analyst"]
        assert excluded == ["sales"]

    def test_lead_hunter_also_requires_sales(self):
        tasks, excluded = enforce_sales_after_hunters(
            [{"task_id": "lh1", "agent_name": "lead_hunter", "depends_on": []}],
            excluded_agents=["sales"],
        )
        assert any(t["agent_name"] == "sales" for t in tasks)
        assert "sales" not in excluded


class TestPreferSingleHunter:
    def test_drops_client_hunter_for_seller_goal(self):
        tasks, excluded = prefer_single_hunter(
            [
                {"task_id": "ch", "agent_name": "client_hunter", "depends_on": []},
                {"task_id": "lh", "agent_name": "lead_hunter", "depends_on": []},
                {"task_id": "s", "agent_name": "sales", "depends_on": ["ch", "lh"]},
            ],
            goal="Найти селлеров Wildberries и Ozon",
        )
        agents = [t["agent_name"] for t in tasks]
        assert "lead_hunter" in agents
        assert "client_hunter" not in agents
        assert "client_hunter" in excluded
        sales = next(t for t in tasks if t["agent_name"] == "sales")
        assert "ch" not in sales["depends_on"]
        assert "lh" in sales["depends_on"]

    def test_drops_lead_hunter_for_clinic_goal(self):
        tasks, excluded = prefer_single_hunter(
            [
                {"task_id": "ch", "agent_name": "client_hunter", "depends_on": []},
                {"task_id": "lh", "agent_name": "lead_hunter", "depends_on": []},
            ],
            goal="Частные стоматологии Москва",
        )
        agents = [t["agent_name"] for t in tasks]
        assert "client_hunter" in agents
        assert "lead_hunter" not in agents
        assert "lead_hunter" in excluded
