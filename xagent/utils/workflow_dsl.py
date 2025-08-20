"""Workflow DSL parsing and validation utilities."""

import re
from typing import Dict, List, Tuple


def parse_dependencies_dsl(dsl_string: str) -> Dict[str, List[str]]:
    """
    Parse DSL string to dependency dictionary.
    
    Supported syntax:
    - A->B: A depends on nothing, B depends on A
    - A->B->C: A->B, B->C (sequential chain)
    - A->B, A->C: A->B and A->C (parallel branches)
    - A&B->C: C depends on both A and B
    - Complex: A->B, A->C, B&C->D
    
    Args:
        dsl_string: DSL string like "A->B, A->C, B&C->D"
        
    Returns:
        Dict mapping agent names to their dependencies
        
    Examples:
        "A->B" -> {"B": ["A"]}
        "A->B->C" -> {"B": ["A"], "C": ["B"]}
        "A->B, A->C" -> {"B": ["A"], "C": ["A"]}
        "A->B, B&C->D" -> {"B": ["A"], "D": ["B", "C"]}
    """
    if not dsl_string or not dsl_string.strip():
        return {}
    
    dependencies = {}
    
    # Split by comma to get individual rules
    rules = [rule.strip() for rule in dsl_string.split(',')]
    
    for rule in rules:
        if not rule:
            continue
        
        # Handle chain syntax (A->B->C becomes A->B, B->C)
        if rule.count('->') > 1:
            # Split into chain segments
            segments = [seg.strip() for seg in rule.split('->')]
            # Create pairs: A->B->C becomes [(A,B), (B,C)]
            for i in range(len(segments) - 1):
                left_part = segments[i]
                right_part = segments[i + 1]
                
                if not right_part:
                    continue
                
                # Parse left part (dependencies) - handle & for multiple dependencies
                if '&' in left_part:
                    deps = [dep.strip() for dep in left_part.split('&')]
                else:
                    deps = [left_part.strip()] if left_part else []
                
                target = right_part.strip()
                
                if target:
                    if target in dependencies:
                        # Merge dependencies if target already exists
                        existing_deps = set(dependencies[target])
                        new_deps = set(deps)
                        dependencies[target] = list(existing_deps.union(new_deps))
                    else:
                        dependencies[target] = deps
        else:
            # Single arrow rule
            if '->' in rule:
                left_part, right_part = rule.split('->', 1)
                left_part = left_part.strip()
                right_part = right_part.strip()
                
                # Parse left part (dependencies) - handle & for multiple dependencies
                if '&' in left_part:
                    deps = [dep.strip() for dep in left_part.split('&')]
                else:
                    deps = [left_part.strip()] if left_part else []
                
                # Parse right part (target) - currently only support single target
                target = right_part.strip()
                
                if target:
                    if target in dependencies:
                        # Merge dependencies if target already exists
                        existing_deps = set(dependencies[target])
                        new_deps = set(deps)
                        dependencies[target] = list(existing_deps.union(new_deps))
                    else:
                        dependencies[target] = deps
    
    return dependencies


def validate_dsl_syntax(dsl_string: str) -> Tuple[bool, str]:
    """
    Validate DSL syntax.
    
    Args:
        dsl_string: DSL string to validate (supports -> arrows)
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not dsl_string or not dsl_string.strip():
        return True, ""
    
    try:
        # Check for valid characters and patterns
        # First check for invalid arrow patterns like --, ->, ->>, etc.
        if '--' in dsl_string or '->>' in dsl_string or '<<-' in dsl_string:
            return False, "Invalid arrow patterns detected. Use -> (single dash followed by >)."
        
        # Check for valid characters (letters, numbers, underscore, arrows, ampersand, comma, space, hyphen, >)
        valid_pattern = re.compile(r'^[a-zA-Z0-9_&,\s\-\>]+$')
        if not valid_pattern.match(dsl_string):
            return False, "Invalid characters in DSL string. Only letters, numbers, underscore, ->, &, comma, and spaces are allowed."
        
        # Split by comma to get individual rules
        rules = [rule.strip() for rule in dsl_string.split(',')]
        for rule in rules:
            if not rule:
                continue
            if '->' not in rule:
                return False, f"Each rule must contain at least one arrow (->). Invalid rule: '{rule}'"
            
            # Handle chain syntax (multiple arrows)
            segments = [seg.strip() for seg in rule.split('->')]
            
            # Check that we don't have empty segments
            for i, segment in enumerate(segments):
                if not segment and i != 0:  # Allow empty first segment (e.g., "->B")
                    return False, f"Empty segment in rule: '{rule}'"
                
                # Check for valid agent names (no empty names after splitting by &)
                if segment and '&' in segment:
                    deps = [dep.strip() for dep in segment.split('&')]
                    for dep in deps:
                        if not dep:
                            return False, f"Empty dependency name in rule: '{rule}'"
        
        # Try to parse to ensure it's valid
        parse_dependencies_dsl(dsl_string)
        return True, ""
        
    except Exception as e:
        return False, f"DSL parsing error: {str(e)}"
