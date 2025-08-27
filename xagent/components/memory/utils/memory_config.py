"""
Memory configuration constants and keyword triggers for local memory storage.
"""

# 最大扫描长度
MAX_SCAN_LENGTH = 1000

# 多层级关键字配置，按重要性和常见性分级，避免不必要的大量监测
TRIGGER_KEYWORDS = [
    # 第一层：高频且重要的记忆触发词 - 对话中经常出现且需要优先记忆
    [
        # 记忆和记录指令 - 用户明确要求存储，最高优先级
        r'记住(?:这|那|一下)?', r'记得', r'请记住', r'帮我记住', r'别忘了',
        r'记一下', r'记下来', r'记录一下', r'保存一下', r'存下来', r'备忘一下', r'写下来',
        r'\bremember\s+this\b', r'\bremember\s+that\b', r'\bplease\s+remember\b', r'\bdon\'?t\s+forget\b',
        r'\bnote\s+this(?:\s+down)?\b', r'\bmake\s+a\s+note\b', r'\bsave\s+this\b', 
        r'\bkeep\s+this\b', r'\blog\s+this\b', r'\bstore\s+this\b', r'\bwrite\s+this\s+down\b',
        
        # 偏好表达 - 对话中高频出现的个人偏好
        r'我.{0,6}喜欢', r'我.{0,6}爱(?!上)', r'我.{0,6}讨厌', r'我.{0,6}不喜欢',
        r'\bi\s+(?:\w+\s+){0,3}like\b', r'\bi\s+(?:\w+\s+){0,3}love\b', r'\bi\s+(?:\w+\s+){0,3}hate\b', r'\bi\s+(?:\w+\s+){0,3}enjoy\b',
        
        # 习惯和特征 - 高频且稳定的个人特征
        r'我.{0,6}习惯', r'我.{0,6}总是', r'我.{0,6}从不', r'我.{0,6}常(?:常)?', r'我.{0,6}经常',
        r'\bi\s+(?:\w+\s+){0,3}usually\b', r'\bi\s+(?:\w+\s+){0,3}always\b', r'\bi\s+(?:\w+\s+){0,3}never\b', r'\bi\s+(?:\w+\s+){0,3}often\b',
        
        # 需求和目标 - 用户的重要需求
        r'我.{0,6}需要', r'我.{0,6}想要', r'我.{0,6}希望', r'我.{0,6}打算', r'我.{0,6}计划',
        r'\bi\s+(?:\w+\s+){0,3}need\b', r'\bi\s+(?:\w+\s+){0,3}want\b', r'\bi\s+(?:\w+\s+){0,3}wish\b', r'\bi\s+(?:\w+\s+){0,2}plan\s+to\b'
    ],
    
    # 第二层：重要但中等频率的个人信息
    [
        # 关键个人信息 - 基本身份信息
        r'我住在', r'我来自', r'我的职业是', r'我叫', r'我的名字是', r'我在.{1,10}工作',
        r'我(?:是|的)?\d{1,3}岁', r'我(?:是|的)?男(?:性)?', r'我(?:是|的)?女(?:性)?',
        r'i\s+live\s+in', r'i\s+am\s+from', r'my\s+job\s+is', r'my\s+name\s+is', r'i\s+work\s+(?:at|for)',
        r'i\s+am\s+\d{1,3}\s+years\s+old', r'i\s+am\s+male', r'i\s+am\s+female',
        
        # 能力和技能 - 用户的专业能力
        r'我.{0,6}擅长', r'我.{0,6}会', r'我.{0,6}不会',
        r'\bi\s+(?:\w+\s+){0,2}am\s+good\s+at\b', r'\bi\s+(?:\w+\s+){0,3}can\b', r'\bi\s+(?:\w+\s+){0,3}cannot\b',
        
        # 个人状态和倾向 - 性格特征描述
        r'我是.{1,10}的人', r'我倾向于',
        r'i\s+am\s+(?:a|an)\s+.{1,15}\s+person', r'i\s+prefer',
        
        # 重要约束和禁忌 - 用户的限制
        r'我不能', r'我不允许', r'我拒绝',
        r'i\s+cannot', r'i\s+refuse\s+to', r'i\s+will\s+not'
    ],
    
    # 第三层：低频但重要的信息
    [
        # 重要经历和事件 - 用户提到的重要经历
        r'我曾经', r'我以前', r'对我来说很重要的是',
        r'i\s+used\s+to', r'i\s+once', r'what\'?s\s+important\s+to\s+me\s+is',
        
        # 情感状态 - 重要的情感表达
        r'我很(?:开心|高兴|难过|伤心|生气|担心|焦虑)', r'让我(?:开心|难过|生气|担心)的是',
        r'i\s+am\s+(?:very\s+)?(?:happy|sad|angry|worried|anxious)'
    ]
]
