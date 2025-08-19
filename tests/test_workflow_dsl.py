#!/usr/bin/env python3
"""
Unit tests for the DSL (Domain Specific Language) functionality in workflow dependencies.
"""

import unittest
import sys
import os

# Add the parent directory to the path so we can import xagent
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xagent.multi.workflow import parse_dependencies_dsl, validate_dsl_syntax


class TestWorkflowDSL(unittest.TestCase):
    """Test cases for workflow DSL parsing and validation."""
    
    def test_basic_parsing(self):
        """Test basic DSL parsing functionality."""
        # Simple dependency
        result = parse_dependencies_dsl("A->B")
        expected = {"B": ["A"]}
        self.assertEqual(result, expected)
        
        # Empty string
        result = parse_dependencies_dsl("")
        self.assertEqual(result, {})
        
        # No dependencies (root node)
        result = parse_dependencies_dsl("->B")
        expected = {"B": []}
        self.assertEqual(result, expected)
    
    def test_chain_parsing(self):
        """Test chain syntax parsing (A->B->C)."""
        result = parse_dependencies_dsl("A->B->C")
        expected = {"B": ["A"], "C": ["B"]}
        self.assertEqual(result, expected)
        
        # Longer chain
        result = parse_dependencies_dsl("A->B->C->D->E")
        expected = {
            "B": ["A"],
            "C": ["B"],
            "D": ["C"],
            "E": ["D"]
        }
        self.assertEqual(result, expected)
    
    def test_parallel_parsing(self):
        """Test parallel dependency parsing (A->B, A->C)."""
        result = parse_dependencies_dsl("A->B, A->C")
        expected = {"B": ["A"], "C": ["A"]}
        self.assertEqual(result, expected)
    
    def test_multiple_dependencies(self):
        """Test multiple dependency parsing (A&B->C)."""
        result = parse_dependencies_dsl("A&B->C")
        expected = {"C": ["A", "B"]}
        self.assertEqual(result, expected)
        
        # More complex
        result = parse_dependencies_dsl("A&B&C->D")
        expected = {"D": ["A", "B", "C"]}
        self.assertEqual(result, expected)
    
    def test_complex_workflows(self):
        """Test complex workflow patterns."""
        # Fan-out then fan-in
        dsl = "A->B, A->C, B&C->D"
        result = parse_dependencies_dsl(dsl)
        expected = {
            "B": ["A"],
            "C": ["A"],
            "D": ["B", "C"]
        }
        self.assertEqual(result, expected)
        
        # Complex real-world example
        dsl = "research->analysis, research->planning, analysis&planning->synthesis, synthesis->review"
        result = parse_dependencies_dsl(dsl)
        expected = {
            "analysis": ["research"],
            "planning": ["research"],
            "synthesis": ["analysis", "planning"],
            "review": ["synthesis"]
        }
        self.assertEqual(result, expected)
    
    def test_dependency_merging(self):
        """Test that multiple rules for the same target merge correctly."""
        # Same target with different dependencies
        dsl = "A->C, B->C"
        result = parse_dependencies_dsl(dsl)
        # Should merge into one dependency list
        self.assertIn("C", result)
        self.assertCountEqual(result["C"], ["A", "B"])
    
    def test_whitespace_handling(self):
        """Test that whitespace is handled correctly."""
        # Various whitespace patterns
        test_cases = [
            "A->B",
            " A -> B ",
            "A->B, A->C",
            " A -> B , A -> C ",
            "A & B -> C",
            " A & B -> C ",
        ]
        
        for dsl in test_cases:
            result = parse_dependencies_dsl(dsl)
            self.assertIsInstance(result, dict)
            # Should not fail and should produce reasonable output
    
    def test_syntax_validation_valid(self):
        """Test validation of valid DSL syntax."""
        valid_cases = [
            "A->B",
            "A->B->C",
            "A->B, A->C",
            "A&B->C",
            "A->B, A->C, B&C->D",
            "->B",  # Root node
            "",    # Empty
        ]
        
        for dsl in valid_cases:
            is_valid, error = validate_dsl_syntax(dsl)
            self.assertTrue(is_valid, f"'{dsl}' should be valid but got error: {error}")
    
    def test_syntax_validation_invalid(self):
        """Test validation of invalid DSL syntax."""
        invalid_cases = [
            "A->",           # Missing target
            "A->B->",         # Incomplete chain
            "A&->B",         # Empty dependency
            "A->B->C->",       # Incomplete end
            "A->B↑C",        # Wrong arrow
            "A->B, ->",       # Incomplete rule
        ]
        
        for dsl in invalid_cases:
            is_valid, error = validate_dsl_syntax(dsl)
            self.assertFalse(is_valid, f"'{dsl}' should be invalid but was accepted")
            self.assertIsInstance(error, str)
            self.assertGreater(len(error), 0)
    
    def test_special_characters_in_names(self):
        """Test agent names with underscores and numbers."""
        dsl = "agent_1->agent_2, agent_2->final_agent"
        result = parse_dependencies_dsl(dsl)
        expected = {
            "agent_2": ["agent_1"],
            "final_agent": ["agent_2"]
        }
        self.assertEqual(result, expected)
    
    def test_real_world_patterns(self):
        """Test real-world workflow patterns."""
        # Research workflow
        research_dsl = "collect_data->analyze_data, collect_data->create_plan, analyze_data&create_plan->write_report"
        result = parse_dependencies_dsl(research_dsl)
        expected = {
            "analyze_data": ["collect_data"],
            "create_plan": ["collect_data"],
            "write_report": ["analyze_data", "create_plan"]
        }
        self.assertEqual(result, expected)
        
        # Software development workflow
        dev_dsl = "requirements->design, requirements->research, design&research->implementation, implementation->testing, testing->deployment"
        result = parse_dependencies_dsl(dev_dsl)
        expected = {
            "design": ["requirements"],
            "research": ["requirements"],
            "implementation": ["design", "research"],
            "testing": ["implementation"],
            "deployment": ["testing"]
        }
        self.assertEqual(result, expected)
    
    def test_both_arrow_types(self):
        """Test that both -> and -> arrows work equivalently."""
        # Test pairs of equivalent DSL strings
        test_pairs = [
            ("A->B", "A->B"),
            ("A->B->C", "A->B->C"),
            ("A->B, A->C", "A->B, A->C"),
            ("A&B->C", "A&B->C"),
            ("A->B, A->C, B&C->D", "A->B, A->C, B&C->D"),
        ]
        
        for unicode_dsl, ascii_dsl in test_pairs:
            unicode_result = parse_dependencies_dsl(unicode_dsl)
            ascii_result = parse_dependencies_dsl(ascii_dsl)
            self.assertEqual(unicode_result, ascii_result, 
                           f"Results should match for '{unicode_dsl}' and '{ascii_dsl}'")
            
            # Both should be valid
            unicode_valid, _ = validate_dsl_syntax(unicode_dsl)
            ascii_valid, _ = validate_dsl_syntax(ascii_dsl)
            self.assertTrue(unicode_valid, f"'{unicode_dsl}' should be valid")
            self.assertTrue(ascii_valid, f"'{ascii_dsl}' should be valid")
    
    def test_mixed_arrow_usage(self):
        """Test mixing both arrow types in the same DSL string."""
        mixed_dsl = "A->B, B->C, C->D"
        result = parse_dependencies_dsl(mixed_dsl)
        expected = {"B": ["A"], "C": ["B"], "D": ["C"]}
        self.assertEqual(result, expected)
        
        is_valid, _ = validate_dsl_syntax(mixed_dsl)
        self.assertTrue(is_valid)
    
    def test_invalid_arrow_patterns(self):
        """Test that invalid arrow patterns are rejected."""
        invalid_patterns = [
            "A-->B",    # Double dash
            "A->>B",    # Dash with double >
            "A<<-B",    # Wrong direction
        ]
        
        for pattern in invalid_patterns:
            is_valid, error = validate_dsl_syntax(pattern)
            self.assertFalse(is_valid, f"'{pattern}' should be invalid but was accepted")
            self.assertIn("arrow", error.lower())
    
    def test_edge_cases(self):
        """Test edge cases and potential issues."""
        # Single agent (no dependencies)
        result = parse_dependencies_dsl("A->")
        is_valid, _ = validate_dsl_syntax("A->")
        self.assertFalse(is_valid)  # Should be invalid
        
        # Circular dependencies (parser should handle, validation elsewhere)
        result = parse_dependencies_dsl("A->B, B->A")
        expected = {"B": ["A"], "A": ["B"]}
        self.assertEqual(result, expected)
        
        # Self-dependency
        result = parse_dependencies_dsl("A->A")
        expected = {"A": ["A"]}
        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
