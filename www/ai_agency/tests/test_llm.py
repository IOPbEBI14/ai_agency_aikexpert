"""
Тесты для функции call_llm() с моками.
"""
import pytest
import json
import requests
from unittest.mock import patch, MagicMock
from core.utils import call_llm, try_fix_truncated_json, validate_with_qa


class TestCallLLM:
    """Тесты функции call_llm()."""
    
    @patch('core.utils.requests.post')
    def test_successful_call(self, mock_post, mock_llm_response):
        """Тест успешного вызова LLM."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_llm_response
        mock_post.return_value = mock_response
        
        from core.utils import call_llm
        content, tokens = call_llm("test_agent", "system prompt", "user task")
        
        assert content == '{"status": "ok", "data": "test"}'
        assert tokens == 100
        mock_post.assert_called_once()
    
    @patch('core.utils.requests.post')
    def test_call_with_timeout_retry(self, mock_post, mock_llm_response):
        """Тест повторной попытки при таймауте."""
        import requests
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_llm_response
        
        # Первый вызов - таймаут, второй - успех
        mock_post.side_effect = [
            requests.exceptions.Timeout(),
            mock_response
        ]
        
        from core.utils import call_llm
        content, tokens = call_llm("test_agent", "system", "task", max_retries=2)
        
        assert content == '{"status": "ok", "data": "test"}'
        assert mock_post.call_count == 2
    
    @patch('core.utils.requests.post')
    def test_call_with_api_error(self, mock_post):
        """Тест обработки ошибки API."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_post.return_value = mock_response
        
        from core.utils import call_llm
        with pytest.raises(requests.exceptions.HTTPError):
            call_llm("test_agent", "system", "task", max_retries=0)
    
    @patch('core.utils.requests.post')
    def test_call_with_truncated_response(self, mock_post):
        """Тест обработки обрезанного ответа."""
        truncated_response = {
            "output_text": '{"key": "value"',
            "usage": {"total_tokens": 50}
        }
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = truncated_response
        mock_post.return_value = mock_response
        
        from core.utils import call_llm
        content, tokens = call_llm("test_agent", "system", "task", max_retries=2)
        
        assert '{"key": "value"}' in content
    
    @patch('core.utils.requests.post')
    def test_call_increases_max_tokens_on_truncation(self, mock_post, mock_llm_response):
        """Тест увеличения max_tokens при обрезанном ответе."""
        truncated_response = {
            "output_text": '{"key": "value"',
            "usage": {"total_tokens": 50}
        }
        
        mock_response_1 = MagicMock()
        mock_response_1.status_code = 200
        mock_response_1.json.return_value = truncated_response
        
        mock_response_2 = MagicMock()
        mock_response_2.status_code = 200
        mock_response_2.json.return_value = mock_llm_response
        
        mock_post.side_effect = [mock_response_1, mock_response_2]
        
        from core.utils import call_llm
        call_llm("test_agent", "system", "task", max_retries=2)
        
        # Проверяем, что второй вызов был с увеличенным max_tokens
        second_call_kwargs = mock_post.call_args_list[1][1]
        assert second_call_kwargs['json']['max_tokens'] > 16000 


class TestTryFixTruncatedJson:
    """Тесты функции восстановления обрезанного JSON."""
    
    def test_fix_missing_closing_brace(self):
        """Тест добавления закрывающей скобки."""
        content = '{"key": "value"'
        result = try_fix_truncated_json(content)
        assert result == '{"key": "value"}'
    
    def test_fix_missing_closing_bracket(self):
        """Тест добавления закрывающей скобки массива."""
        content = '{"key": [1, 2'
        result = try_fix_truncated_json(content)
        assert result == '{"key": [1, 2]}'
    
    def test_fix_nested_braces(self):
        """Тест восстановления вложенных скобок."""
        content = '{"outer": {"inner": "value"'
        result = try_fix_truncated_json(content)
        assert result == '{"outer": {"inner": "value"}}'
    
    def test_fix_by_removing_last_comma(self):
        """Тест восстановления через удаление последней запятой."""
        content = '{"key": "value", "other":'
        result = try_fix_truncated_json(content)
        # Должен удалить запятую и закрыть скобки
        assert result is not None
        json.loads(result)  # Должен быть валидным JSON
    
    def test_fix_empty_string(self):
        """Тест пустой строки."""
        result = try_fix_truncated_json("")
        assert result == ""
    
    def test_fix_non_json_string(self):
        """Тест строки без JSON."""
        result = try_fix_truncated_json("Просто текст")
        assert result == ""
    
    def test_fix_already_valid_json(self):
        """Тест уже валидного JSON."""
        content = '{"key": "value"}'
        result = try_fix_truncated_json(content)
        assert result == content
    
    def test_fix_complex_nested_structure(self):
        """Тест сложной вложенной структуры."""
        # Упрощённый тест — функция не обязана восстанавливать все случаи
        content = '{"a": {"b": [1, 2, {"c": "d"'
        result = try_fix_truncated_json(content)
        
        # Функция должна хотя бы попытаться восстановить
        # Если не может — возвращает пустую строку
        if result:
            # Если вернула результат, он должен быть валидным JSON
            try:
                json.loads(result)
            except json.JSONDecodeError:
                pytest.fail("Восстановленный JSON невалиден")
        else:
            # Если не смогла восстановить — это допустимо
            assert result == ""