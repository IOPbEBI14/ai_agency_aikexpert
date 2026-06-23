"""
Тесты для функции validate_with_qa().
"""
import pytest
from unittest.mock import patch, MagicMock
from core.utils import validate_with_qa


class TestValidateWithQA:
    """Тесты функции validate_with_qa()."""
    
    @patch('main.call_llm')
    @patch('main.load_prompt')
    def test_qa_approved(self, mock_load_prompt, mock_call_llm):
        """Тест одобренного QA."""
        mock_load_prompt.return_value = "QA промпт"
        
        # validate_with_qa парсит JSON и возвращает dict
        # Mock должен возвращать JSON, который можно распарсить
        mock_call_llm.return_value = (
            '{"approved": true, "feedback": "Всё отлично", "issues": []}',
            100
        )
        
        result = validate_with_qa("analyst", '{"data": "value"}', "Описание задачи")
        
        assert result["approved"] is True
        assert result["tokens_used"] == 100
        
        # Проверяем, что call_llm был вызван хотя бы один раз
        assert mock_call_llm.call_count >= 1
    
    @patch('main.call_llm')
    @patch('main.load_prompt')
    def test_qa_rejected(self, mock_load_prompt, mock_call_llm):
        """Тест отклонённого QA."""
        mock_load_prompt.return_value = "QA промпт"
        mock_call_llm.return_value = (
            '{"approved": false, "feedback": "Найдены ошибки", "issues": ["Ошибка 1"]}',
            100
        )
        
        result = validate_with_qa("analyst", '{"data": "value"}', "Описание задачи")
        
        assert result["approved"] is False
        assert result["feedback"] == "Найдены ошибки"
    
    @patch('main.call_llm')
    @patch('main.load_prompt')
    def test_qa_with_llm_error(self, mock_load_prompt, mock_call_llm):
        """Тест ошибки LLM при QA."""
        mock_load_prompt.return_value = "QA промпт"
        mock_call_llm.side_effect = Exception("LLM ошибка")
        
        result = validate_with_qa("analyst", '{"data": "value"}', "Описание задачи")
        
        # При ошибке должен вернуть approved=True (пропускаем)
        assert result["approved"] is True
        assert "QA ошибка" in result["feedback"]
    
    @patch('main.call_llm')
    @patch('main.load_prompt')
    def test_qa_with_invalid_json(self, mock_load_prompt, mock_call_llm):
        """Тест невалидного JSON от QA."""
        mock_load_prompt.return_value = "QA промпт"
        mock_call_llm.return_value = ("Невалидный JSON", 100)
        
        result = validate_with_qa("analyst", '{"data": "value"}', "Описание задачи")
        
        # При невалидном JSON должен вернуть approved=True
        assert result["approved"] is True
    
    @patch('main.call_llm')
    @patch('main.load_prompt')
    def test_qa_passes_long_response(self, mock_load_prompt, mock_call_llm):
        """Тест QA с длинным ответом агента."""
        mock_load_prompt.return_value = "QA промпт"
        mock_call_llm.return_value = (
            '{"approved": true, "feedback": "OK", "issues": []}',
            100
        )
        
        # Создаём длинный ответ (7000 символов)
        long_response = '{"data": "' + 'x' * 7000 + '"}'
        
        result = validate_with_qa("analyst", long_response, "Описание задачи")
        
        assert result["approved"] is True
        # Проверяем, что ответ был обрезан до 6000 символов
        call_args = mock_call_llm.call_args
        assert len(call_args[0][2]) < 7000  # user_task должен быть обрезан
    
    @patch('main.call_llm')
    @patch('main.load_prompt')
    def test_qa_with_complex_issues(self, mock_load_prompt, mock_call_llm):
        """Тест QA со сложными проблемами."""
        mock_load_prompt.return_value = "QA промпт"
        mock_call_llm.return_value = (
            '''{
                "approved": false,
                "feedback": "Критические ошибки",
                "issues": [
                    {
                        "severity": "critical",
                        "type": "validation",
                        "description": "Невалидный JSON",
                        "location": "Node 1",
                        "recommendation": "Исправить"
                    }
                ],
                "warnings": ["Предупреждение"],
                "recommendations": ["Рекомендация"]
            }''',
            150
        )
        
        result = validate_with_qa("developer", '{"workflow": "..."}', "Создать workflow")
        
        assert result["approved"] is False
        assert len(result["issues"]) == 1
        assert result["issues"][0]["severity"] == "critical"